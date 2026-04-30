import zipfile
import re
import uuid
import io
import img2pdf
from pathlib import Path
from typing import Union
from PIL import Image
from PyPDF2 import PdfWriter, PdfReader
from nonebot.log import logger

# 支持的图片格式
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff'}

class PDFUtils:
    """
    PDF 处理工具类
    流程：ZIP -> Pillow(压缩/缩放) -> JPEG Bytes -> img2pdf -> PDF
    """

    @staticmethod
    def natural_sort_key(filename: str):
        """自然排序算法"""
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', filename)]

    @staticmethod
    def convert_zip_to_pdf(
        zip_path: Union[str, Path], 
        output_dir: Union[str, Path], 
        compress_level: int = 80,  # JPEG 质量 (1-100)
        max_width: int = 1920      # 最大宽度，超过则缩小 (None 表示不缩放)
    ) -> str:
        """
        1. 解压图片
        2. 使用 Pillow 缩放并转为 JPEG (降低体积)
        3. 使用 img2pdf 合成 PDF
        """
        zip_path = Path(zip_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_pdf = output_dir / (zip_path.stem + ".pdf")
        
        # 存储处理后的图片二进制流
        processed_buffers = []

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # 1. 筛选并排序
                all_files = zf.infolist()
                image_files = []
                for zi in all_files:
                    if not zi.is_dir() and Path(zi.filename).suffix.lower() in IMAGE_EXTS:
                        image_files.append(zi)
                
                if not image_files:
                    logger.warning(f"[PDFUtils] ZIP 中未找到有效图片: {zip_path}")
                    return ""

                image_files.sort(key=lambda zi: PDFUtils.natural_sort_key(zi.filename))
                
                total = len(image_files)
                logger.info(f"[PDFUtils] 开始处理: {zip_path.name} ({total} 张) | 质量: {compress_level} | 宽限: {max_width}")

                # 2. 逐张处理 (Pillow 压缩)
                for i, zi in enumerate(image_files):
                    try:
                        raw_data = zf.read(zi)
                        img = Image.open(io.BytesIO(raw_data))
                        
                        # 转 RGB (img2pdf 和 JPEG 都不支持 RGBA)
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        
                        # 缩放 (Resize)
                        if max_width and img.width > max_width:
                            ratio = max_width / img.width
                            new_height = int(img.height * ratio)
                            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                        
                        # 压缩保存到内存 (Save as JPEG)
                        # 这里将图片重编码为 JPEG 格式，从而实现体积缩减
                        new_buffer = io.BytesIO()
                        img.save(new_buffer, format="JPEG", quality=compress_level, optimize=True)
                        new_buffer.seek(0) # 指针归位
                        
                        processed_buffers.append(new_buffer)
                        
                    except Exception as img_e:
                        logger.warning(f"[PDFUtils] 图片处理跳过 {zi.filename}: {img_e}")
                        continue

                if not processed_buffers:
                    return ""

                # 3. 合成 PDF (img2pdf)
                logger.info(f"[PDFUtils] 图片处理完毕，正在打包 PDF...")
                
                # img2pdf 接受 bytes 列表
                pdf_content = img2pdf.convert(processed_buffers)
                
                with open(out_pdf, "wb") as f:
                    f.write(pdf_content)
                
                # 清理内存
                for b in processed_buffers:
                    b.close()

                # 统计
                size_mb = out_pdf.stat().st_size / 1024 / 1024
                logger.info(f"[PDFUtils] ✅ 转换完成: {out_pdf.name} | 大小: {size_mb:.2f}MB")
                return str(out_pdf)

        except Exception as e:
            logger.error(f"[PDFUtils] 任务失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return ""

    @staticmethod
    def modify_pdf_metadata(input_pdf: Union[str, Path], output_pdf: Union[str, Path]) -> bool:
        """
        混淆元数据 (保持不变)
        """
        try:
            input_pdf = Path(input_pdf)
            output_pdf = Path(output_pdf)
            
            if not input_pdf.exists():
                return False

            reader = PdfReader(str(input_pdf))
            writer = PdfWriter()
            writer.append_pages_from_reader(reader)
            
            unique_id = str(uuid.uuid4())
            new_metadata = {
                "/Title": f"Doc_{unique_id[:8]}",
                "/Author": "Reader",
                "/Producer": "Generic PDF Library",
                "/Creator": f"Bot_v{uuid.uuid4().hex[:4]}",
                "/Keywords": unique_id
            }
            
            writer.add_metadata(new_metadata)
            output_pdf.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_pdf, "wb") as f:
                writer.write(f)
            
            return True
        except Exception as e:
            logger.error(f"[PDFUtils] Metadata error: {e}")
            return False