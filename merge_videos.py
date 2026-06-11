#!/usr/bin/env python3
"""
视频拼接脚本
用法:
    python merge_videos.py folder/           # 拼接文件夹内所有视频（按时间顺序）
    python merge_videos.py a.mp4 b.mp4 c.mp4 # 拼接指定多个文件
    python merge_videos.py a.mp4 b.mp4 -o output.mp4 # 指定输出文件
输出: 默认生成 _merge.mp4 结尾的文件
"""

import subprocess
import sys
import os
import time
import tempfile
from pathlib import Path
from typing import List, Tuple

def get_video_files_from_folder(folder_path: str) -> List[Path]:
    """获取文件夹中所有视频文件，按修改时间排序"""
    folder = Path(folder_path)
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder_path} 不是有效的文件夹")
    
    # 常见视频扩展名
    video_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm', '.m4v'}
    video_files = [f for f in folder.iterdir() 
                   if f.is_file() and f.suffix.lower() in video_extensions]
    
    if not video_files:
        return []
    
    # 按修改时间排序
    video_files.sort(key=lambda f: f.stat().st_mtime)
    return video_files

def check_ffmpeg():
    """检查 ffmpeg 是否可用"""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False

def get_video_duration(video_path: Path) -> float:
    """使用 ffprobe 获取视频时长（秒）"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        print(f"警告: 无法读取视频时长 - {video_path}")
        return 0.0

def format_time(seconds: float) -> str:
    """格式化秒数为 HH:MM:SS"""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def parse_ffmpeg_progress_time(line: str):
    """解析 ffmpeg -progress 输出中的处理时间"""
    if "=" not in line:
        return None

    key, value = line.strip().split("=", 1)
    try:
        if key in {"out_time_us", "out_time_ms"}:
            return int(value) / 1_000_000
        if key == "out_time":
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, TypeError):
        return None

    return None

def print_progress(current: float, total: float, start_time: float, label: str = "进度"):
    """打印单行实时进度条和预计处理时间"""
    elapsed = time.monotonic() - start_time

    if total <= 0:
        line = (
            f"\r{label}: 已处理 {format_time(current)} | "
            f"已用 {format_time(elapsed)} | 预计剩余 --:--"
        )
        print(line, end="", flush=True)
        return

    current = min(max(current, 0.0), total)
    percent = current / total
    bar_width = 30
    filled = int(bar_width * percent)
    bar = "#" * filled + "-" * (bar_width - filled)

    if percent > 0:
        estimated_total = elapsed / percent
        remaining = max(estimated_total - elapsed, 0.0)
        estimated_total_text = format_time(estimated_total)
        remaining_text = format_time(remaining)
    else:
        estimated_total_text = "--:--"
        remaining_text = "--:--"

    line = (
        f"\r{label}: [{bar}] {percent * 100:5.1f}% "
        f"({format_time(current)}/{format_time(total)}) | "
        f"已用 {format_time(elapsed)} | "
        f"预计剩余 {remaining_text} | "
        f"预计总耗时 {estimated_total_text}"
    )
    print(line + " " * 10, end="", flush=True)

def run_ffmpeg_with_progress(cmd: List[str], total_duration: float) -> Tuple[int, str]:
    """执行 ffmpeg 并实时显示处理进度"""
    progress_cmd = [
        cmd[0],
        "-nostats",
        "-loglevel", "error",
        "-progress", "pipe:1",
        *cmd[1:]
    ]

    start_time = time.monotonic()
    stderr_text = ""
    process = subprocess.Popen(
        progress_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )

    try:
        if process.stdout:
            for raw_line in process.stdout:
                processed_seconds = parse_ffmpeg_progress_time(raw_line)
                if processed_seconds is not None:
                    print_progress(processed_seconds, total_duration, start_time)

        if process.stderr:
            stderr_text = process.stderr.read()
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        print()
        raise

    if return_code == 0:
        print_progress(total_duration, total_duration, start_time)
    print()
    return return_code, stderr_text

def generate_file_list(video_paths: List[Path]) -> str:
    """生成 ffmpeg concat 需要的临时文件列表内容"""
    lines = []
    for p in video_paths:
        # 转义路径中的特殊字符
        escaped_path = str(p).replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'")
    return "\n".join(lines)

def create_temp_file_list(video_paths: List[Path], output_dir: Path) -> Path:
    """创建本次运行专属的 ffmpeg concat 临时文件，避免多脚本并发冲突"""
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, list_file_name = tempfile.mkstemp(
        prefix=f"ffmpeg_concat_{os.getpid()}_",
        suffix=".txt",
        dir=str(output_dir),
        text=True,
    )
    list_file = Path(list_file_name)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(generate_file_list(video_paths))
    return list_file

def safe_delete_temp_file(list_file: Path):
    """尽量删除临时文件；Windows 下文件被占用时不影响主流程"""
    try:
        if list_file.exists():
            list_file.unlink()
    except PermissionError:
        print(f"警告: 临时文件仍被占用，稍后可手动删除: {list_file}")
    except OSError as exc:
        print(f"警告: 无法删除临时文件 {list_file}: {exc}")

def merge_videos(video_paths: List[Path], output_path: Path) -> bool:
    """使用 ffmpeg 拼接视频"""
    if len(video_paths) == 0:
        print("错误: 没有找到任何视频文件")
        return False
    
    print(f"找到 {len(video_paths)} 个视频文件:")
    for p in video_paths:
        print(f"  - {p}")

    total_duration = sum(get_video_duration(p) for p in video_paths)
    if total_duration > 0:
        print(f"视频总时长: {format_time(total_duration)}")
        print("预计处理时间将根据实时处理速度动态估算")
    
    # 创建本次运行专属的临时文件列表，支持多个脚本在同一目录同时运行。
    list_file = create_temp_file_list(video_paths, output_path.parent)
    
    # ffmpeg 拼接命令
    cmd = [
        "ffmpeg",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-y",  # 覆盖输出文件
        str(output_path)
    ]
    
    print(f"\n开始拼接，输出文件: {output_path}")
    
    try:
        return_code, stderr_text = run_ffmpeg_with_progress(cmd, total_duration)
        # 删除临时文件
        safe_delete_temp_file(list_file)
        
        if return_code != 0:
            print("FFmpeg 错误:")
            print(stderr_text)
            return False
        
        # 获取文件大小
        file_size = output_path.stat().st_size / (1024 * 1024)
        print(f"✓ 拼接成功! 输出文件: {output_path} ({file_size:.2f} MB)")
        return True
        
    except Exception as e:
        print(f"执行出错: {e}")
        safe_delete_temp_file(list_file)
        return False

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="将多个视频文件拼接成一个文件",
        usage="%(prog)s [输入文件或文件夹...] [-o 输出文件]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python merge_videos.py folder/                    # 拼接文件夹内所有视频
  python merge_videos.py a.mp4 b.mp4 c.mp4         # 拼接指定文件
  python merge_videos.py a.mp4 b.mp4 -o out.mp4    # 指定输出文件名
  python merge_videos.py .                         # 拼接当前文件夹所有视频
        """
    )
    parser.add_argument(
        "inputs", 
        nargs="+",
        help="输入: 文件夹路径 或 视频文件路径"
    )
    parser.add_argument(
        "-o", "--output", 
        help="输出文件名 (默认: 根据输入自动生成)"
    )
    
    # 兼容旧用法：如果没有参数，显示帮助
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    
    args = parser.parse_args()
    
    # 检查 ffmpeg
    if not check_ffmpeg():
        print("错误: 未找到 ffmpeg，请先安装 ffmpeg")
        print("  Ubuntu/Debian: sudo apt install ffmpeg")
        print("  macOS: brew install ffmpeg")
        print("  Windows: 下载 https://ffmpeg.org/download.html")
        sys.exit(1)
    
    # 解析输入
    video_files: List[Path] = []
    
    # 判断第一个参数是文件夹还是文件列表
    if len(args.inputs) == 1 and Path(args.inputs[0]).is_dir():
        # 文件夹模式
        folder_path = args.inputs[0]
        video_files = get_video_files_from_folder(folder_path)
        if not video_files:
            print(f"错误: 文件夹 '{folder_path}' 中没有找到视频文件")
            sys.exit(1)
        
        # 默认输出文件名: 文件夹名_merge.mp4
        if not args.output:
            folder_name = Path(folder_path).name
            output_path = Path(folder_path) / f"{folder_name}_merge.mp4"
        else:
            output_path = Path(args.output)
    else:
        # 多个文件模式
        for f in args.inputs:
            p = Path(f)
            if not p.is_file():
                print(f"错误: 文件不存在 - {f}")
                sys.exit(1)
            video_files.append(p)
        
        if not video_files:
            print("错误: 没有提供有效的视频文件")
            sys.exit(1)
        
        # 默认输出文件名: 基于第一个文件生成
        if not args.output:
            first_stem = video_files[0].stem
            # 移除可能的 _merge 后缀
            if first_stem.endswith("_merge"):
                first_stem = first_stem[:-6]
            output_path = video_files[0].parent / f"{first_stem}_merge.mp4"
        else:
            output_path = Path(args.output)
    
    # 确保输出目录存在
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 执行拼接
    success = merge_videos(video_files, output_path)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()