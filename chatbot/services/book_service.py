# src/plugins/chatbot/services/book_service.py

import os
import asyncio
import zipfile
import uuid
import shutil
from pathlib import Path
from typing import List, Optional, Dict, Any
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot

# 加密依赖
try:
    from PyPDF2 import PdfReader, PdfWriter
except ImportError:
    PdfReader = None
    PdfWriter = None

from ..config import plugin_config
from ..repositories.book_repo import BookRepository
from ..utils.pdf_utils import PDFUtils

# JM 依赖
try:
    import jmcomic
except ImportError:
    jmcomic = None
    logger.warning("未检测到 jmcomic 库，JM 下载功能将不可用")


class BookService:
    """
    书籍业务服务
    职责：
    1. 异步调度 JM 下载（接单 -> 后台处理 -> 异步回调）。
    2. 协调 PDF 转换、加密、发送流程。
    3. 处理具体的业务彩蛋（苦命鸳鸯）。
    """

    def __init__(self):
        self.repo = BookRepository()
        # 临时下载缓存目录 (JM配置用)
        self.temp_dir = Path(plugin_config.jm_download_dir)
        self.option_yaml_path = Path(plugin_config.jm_option_path)

        self.temp_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    async def _send_message(bot: Bot, target_id: int, message_type: str, message: str):
        """统一的消息发送封装"""
        if message_type == "group":
            await bot.send_group_msg(group_id=target_id, message=message)
        else:
            await bot.send_private_msg(user_id=target_id, message=message)

    # ================================================================
    #  异步解耦入口：接单 -> 后台任务 -> 自动回调
    # ================================================================

    async def enqueue_jm_download(
        self, bot: Bot, target_id: int, message_type: str, ids: List[str], user_id: str
    ) -> str:
        """
        [新入口] 异步解耦下载。
        立即返回确认消息，后台启动下载任务。
        """
        if not self._check_env():
            return "❌ 环境配置不完整 (缺少库或 option.yml)"

        if not ids:
            return "❌ 请提供 ID"

        logger.info(f"[JM] 接单: {ids}, target={target_id}, type={message_type}")

        # 启动后台任务，不等待完成
        asyncio.create_task(
            self._background_download_and_send(bot, target_id, message_type, ids, user_id)
        )

        id_str = ", ".join(ids)
        return f"✅ 已将 [禁漫ID: {id_str}] 加入后台下载队列，完成后将自动发送，请不要重复下达指令。"

    async def _background_download_and_send(
        self,
        bot: Bot,
        target_id: int,
        message_type: str,
        ids: List[str],
        user_id: str,
    ):
        """
        后台下载 -> 打包 -> 发送 -> 清理 -> 通知。
        全程不阻塞 NoneBot 事件循环。
        """
        try:
            # 1. 下载（线程池隔离，不阻塞事件循环）
            downloaded_items = await asyncio.to_thread(self._sync_download_task, ids)

            if not downloaded_items:
                await self._send_message(bot, target_id, message_type, "❌ 下载失败或无文件生成。")
                return

            # 2. 批量处理并发送（PDF转换 + 加密 + 上传）
            result_msg = await self._batch_process_and_send(bot, target_id, message_type, downloaded_items)

            # 3. 完成通知（@用户）
            at_segment = f"[CQ:at,qq={user_id}]" if message_type == "group" else ""
            await self._send_message(bot, target_id, message_type, f"{at_segment} 您点播的漫画下载完毕！\n{result_msg}")

        except Exception as e:
            logger.error(f"[JM] 后台任务异常: {e}")
            try:
                await self._send_message(bot, target_id, message_type, f"❌ 后台下载任务异常: {e}")
            except Exception:
                pass

    # ================================================================
    #  兼容旧入口（苦命鸳鸯彩蛋仍用同步模式）
    # ================================================================

    async def handle_jm_download(self, bot: Bot, target_id: int, message_type: str, ids: List[str]) -> str:
        """
        [旧入口] 同步下载（仅供彩蛋等内部调用）。
        """
        if not self._check_env():
            return "❌ 环境配置不完整 (缺少库或 option.yml)"

        if not ids:
            return "❌ 请提供 ID"

        logger.info(f"[JM] 同步下载任务: {ids}")

        # 1. 下载 (返回: [{'id':..., 'path':...}])
        downloaded_items = await self._run_sync_download(ids)
        if not downloaded_items:
            return "❌ 下载失败或无文件生成。"

        # 2. 批量处理并发送
        return await self._batch_process_and_send(bot, target_id, message_type, downloaded_items)

    async def handle_bitter_lovebirds(self, bot: Bot, group_id: int) -> str:
        """
        [入口] 苦命鸳鸯彩蛋 (350234, 350235)
        """
        if not self._check_env():
            return "❌ 环境不支持，无法触发彩蛋。"

        target_ids = ['350234', '350235']
        final_items = []
        missing_ids = []

        logger.info(f"[JM] 触发苦命鸳鸯彩蛋 check: {target_ids}")

        # 1. 检查本地库存
        for tid in target_ids:
            local_path = self.repo.find_book_by_id_or_name(tid)
            if local_path:
                final_items.append({
                    'id': tid,
                    'title': local_path.stem,
                    'path': local_path
                })
                logger.info(f"[JM] 本地命中彩蛋资源: {local_path.name}")
            else:
                missing_ids.append(tid)

        # 2. 下载缺失的
        if missing_ids:
            logger.info(f"[JM] 本地缺失，开始下载: {missing_ids}")
            downloaded = await self._run_sync_download(missing_ids)
            final_items.extend(downloaded)

        if not final_items:
            return "❌ 苦命鸳鸯彻底走散了... (无法获取资源)"

        # 3. 统一发送
        await self._batch_process_and_send(bot, group_id, "group", final_items)

        return "…这何尝不是一种苦命鸳鸯"

    # ================================================================
    #  核心流程
    # ================================================================

    async def _batch_process_and_send(self, bot: Bot, target_id: int, message_type: str, items: List[Dict[str, Any]]) -> str:
        """核心流程：转换 -> 注入随机UUID并加密 -> 发送 -> 清理临时文件"""
        loop = asyncio.get_running_loop()
        success_count = 0
        failed_ids = []

        # 告知密码
        msg_text = "🔒 文件正在加密处理中...\n🔑 统一密码：114514"
        await self._send_message(bot, target_id, message_type, msg_text)

        for item in items:
            book_id = item['id']
            source_path = item['path']

            # --- Step 1: 确保是 PDF ---
            if source_path.suffix.lower() != '.pdf':
                expected_pdf_path = self.repo.get_pdf_output_path(source_path)
                if expected_pdf_path.exists():
                    target_pdf = expected_pdf_path
                else:
                    logger.info(f"[JM] 转换格式: {source_path.name} -> PDF")
                    result_str = await loop.run_in_executor(
                        None,
                        PDFUtils.convert_zip_to_pdf,
                        str(source_path),
                        str(self.repo.output_dir)
                    )
                    if result_str and Path(result_str).exists():
                        target_pdf = Path(result_str)
                    else:
                        logger.warning(f"[JM] 转换失败，跳过: {source_path.name}")
                        failed_ids.append(book_id)
                        continue
            else:
                target_pdf = source_path

            # --- Step 2: 注入UUID + 加密 ---
            ready_to_send = target_pdf
            is_temp_encrypted_file = False

            if ready_to_send.suffix.lower() == '.pdf':
                temp_filename = f"enc_{uuid.uuid4().hex[:8]}_{target_pdf.name}"
                temp_enc_path = self.temp_dir / temp_filename

                logger.info(f"[JM] 正在处理(混淆MD5+加密): {target_pdf.name}")
                final_enc_path = await loop.run_in_executor(
                    None,
                    self._encrypt_pdf_task,
                    target_pdf,
                    temp_enc_path,
                    "114514"
                )
                if final_enc_path and final_enc_path.exists():
                    ready_to_send = final_enc_path
                    is_temp_encrypted_file = True

            # --- Step 3: 发送 (动态超时) ---
            if not ready_to_send.exists():
                logger.error(f"[JM] 文件不存在: {ready_to_send}")
                failed_ids.append(book_id)
                continue

            file_size = ready_to_send.stat().st_size
            speed = 50 * 1024
            timeout = 30 + (file_size / speed)
            logger.info(f"[JM] 发送: {ready_to_send.name} | Size: {file_size/1024/1024:.1f}MB | Timeout: {timeout:.0f}s")

            try:
                api_name = "upload_group_file" if message_type == "group" else "upload_private_file"
                api_kwargs: Dict[str, Any] = {
                    "file": str(ready_to_send.absolute()),
                    "name": target_pdf.name,
                }
                if message_type == "group":
                    api_kwargs["group_id"] = target_id
                else:
                    api_kwargs["user_id"] = target_id

                upload_coro = bot.call_api(api_name, **api_kwargs)
                await asyncio.wait_for(upload_coro, timeout=timeout)
                success_count += 1
            except asyncio.TimeoutError:
                logger.error(f"[JM] 上传超时 {book_id}")
                failed_ids.append(book_id)
            except Exception as e:
                logger.error(f"[JM] 上传API失败 {book_id}: {e}")
                failed_ids.append(book_id)
            finally:
                # --- Step 4: 清理临时加密文件 ---
                if is_temp_encrypted_file and ready_to_send.exists():
                    try:
                        ready_to_send.unlink()
                        logger.debug(f"[JM] 已删除临时加密文件: {ready_to_send.name}")
                    except Exception as del_err:
                        logger.warning(f"[JM] 删除临时文件失败: {del_err}")

        # 汇总消息
        msg = f"✅ 任务结束。发送 {success_count}/{len(items)} 本。"
        if failed_ids:
            msg += f"\n❌ 失败ID: {', '.join(failed_ids)}"
        return msg

    def _encrypt_pdf_task(self, input_path: Path, output_path: Path, password: str) -> Optional[Path]:
        """同步任务：注入随机UUID元数据并加密"""
        if not PdfWriter:
            return None
        try:
            reader = PdfReader(str(input_path))
            writer = PdfWriter()

            for page in reader.pages:
                writer.add_page(page)

            random_uid = str(uuid.uuid4())
            metadata = reader.metadata
            new_metadata = {k: v for k, v in metadata.items()} if metadata else {}
            new_metadata['/Custom-UUID'] = random_uid
            new_metadata['/Producer'] = f"JM-Bot-{random_uid[:8]}"

            writer.add_metadata(new_metadata)
            writer.encrypt(password)

            with open(output_path, "wb") as f:
                writer.write(f)

            return output_path
        except Exception as e:
            logger.error(f"加密/混淆失败: {e}")
            return None

    # ================================================================
    #  下载引擎（线程池隔离）
    # ================================================================

    async def _run_sync_download(self, ids: List[str]) -> List[Dict[str, Any]]:
        """执行下载任务 (线程池)"""
        return await asyncio.to_thread(self._sync_download_task, ids)

    def _sync_download_task(self, ids: List[str]) -> List[Dict[str, Any]]:
        """
        JM 下载逻辑实现（同步，在线程池中运行）。
        强制注入 Clash 代理，确保网络可达。
        """
        results = []
        try:
            option = jmcomic.JmOption.from_file(str(self.option_yaml_path))
            option.dir_rule.base_dir = str(self.temp_dir)

            # ===== 强制注入 Clash 代理 =====
            try:
                option.client.proxies = {
                    "http": "http://127.0.0.1:7890",
                    "https": "http://127.0.0.1:7890",
                }
            except Exception as proxy_err:
                logger.warning(f"[JM] 代理注入失败(非致命): {proxy_err}")
            # ==============================

            downloader = jmcomic.JmDownloader(option)

            for album_id in ids:
                try:
                    # 0. 先检查本地是否已有
                    existing_book = self.repo.find_book_by_id_or_name(str(album_id))
                    if existing_book:
                        logger.info(f"[JM] 本地已存在，跳过下载: {existing_book.name}")
                        results.append({
                            'id': str(album_id),
                            'title': existing_book.stem,
                            'path': existing_book
                        })
                        continue

                    # 尝试获取标题
                    try:
                        album = downloader.client.get_album_detail(album_id)
                        title = album.title
                    except Exception:
                        title = f"JM_{album_id}"

                    # 1. 检查下载缓存
                    chapter_dirs = self._find_chapter_dirs(album_id)
                    if not chapter_dirs:
                        logger.info(f"[JM] 下载中: {album_id}")
                        downloader.download_album(album_id)
                        chapter_dirs = self._find_chapter_dirs(album_id)

                    if not chapter_dirs:
                        logger.warning(f"[JM] 未找到下载内容: {album_id}")
                        continue

                    # 2. 打包 ZIP
                    for c_dir in chapter_dirs:
                        c_name = os.path.basename(c_dir)
                        zip_path = self.repo.books_dir / f"{c_name}.zip"

                        if not zip_path.exists():
                            self._zip_folder(c_dir, zip_path)

                            if zip_path.exists():
                                try:
                                    shutil.rmtree(c_dir)
                                    logger.info(f"[JM] ZIP打包完成，已清理源文件: {c_name}")
                                except Exception as e:
                                    logger.warning(f"[JM] 清理源文件失败 {c_name}: {e}")

                        if zip_path.exists():
                            results.append({
                                'id': str(album_id),
                                'title': title,
                                'path': zip_path
                            })

                except Exception as e:
                    logger.error(f"[JM] Item Error {album_id}: {e}")
        except Exception as e:
            logger.error(f"[JM] Setup Error: {e}")

        return results

    # ================================================================
    #  辅助方法
    # ================================================================

    def _find_chapter_dirs(self, aid: str) -> List[str]:
        """查找临时目录下的章节文件夹"""
        found = []
        if self.temp_dir.exists():
            for d in os.listdir(self.temp_dir):
                full = self.temp_dir / d
                if str(aid) in d and full.is_dir():
                    found.append(str(full))
        return found

    def _zip_folder(self, folder_path: str, output_path: Path):
        """打包文件夹为 ZIP"""
        try:
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(folder_path):
                    for file in files:
                        p = os.path.join(root, file)
                        arcname = os.path.relpath(p, os.path.dirname(folder_path))
                        zf.write(p, arcname)
        except Exception as e:
            logger.error(f"ZIP Error: {e}")

    def _check_env(self) -> bool:
        return (jmcomic is not None) and self.option_yaml_path.exists()
