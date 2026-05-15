# del_pyc.py
import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def clean_pyc_and_pycache(root):
    for cur_dir, dirs, files in os.walk(root, topdown=False):
        # 删除 .pyc 文件
        for file in files:
            if file.endswith(".pyc"):
                path = os.path.join(cur_dir, file)
                try:
                    os.remove(path)
                    print(f"删除文件: {path}")
                except Exception as e:
                    print(f"无法删除 {path}: {e}")

        # 删除 __pycache__ 目录
        for dir_name in dirs:
            if dir_name == "__pycache__":
                cache_dir = os.path.join(cur_dir, dir_name)
                try:
                    shutil.rmtree(cache_dir)
                    print(f"删除目录: {cache_dir}")
                except Exception as e:
                    print(f"无法删除 {cache_dir}: {e}")


if __name__ == "__main__":
    print(f"清理目录: {BASE_DIR}")
    clean_pyc_and_pycache(BASE_DIR)
    print("✅ 清理完成")