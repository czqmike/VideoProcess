import os
import sys
import math
import subprocess
import time

def format_time(seconds):
    """格式化秒数为 HH:MM:SS"""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def parse_ffmpeg_progress_time(line):
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

def print_progress(current, total, start_time, label="总体进度"):
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

def normalize_segment_progress(processed_seconds, segment_duration, expected_start):
    """兼容 ffmpeg 可能输出相对时间或原视频时间戳的情况"""
    if processed_seconds > segment_duration and processed_seconds >= expected_start:
        processed_seconds -= expected_start
    return min(max(processed_seconds, 0.0), segment_duration)

def run_ffmpeg_with_progress(cmd, segment_duration, total_duration, completed_duration, start_time, label, expected_start=0.0):
    """执行 ffmpeg 并按总任务显示实时进度"""
    progress_cmd = [
        cmd[0],
        "-nostats",
        "-loglevel", "error",
        "-progress", "pipe:1",
        *cmd[1:]
    ]

    stderr_text = ""
    process = subprocess.Popen(
        progress_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )

    try:
        last_segment_progress = 0.0
        if process.stdout:
            for raw_line in process.stdout:
                processed_seconds = parse_ffmpeg_progress_time(raw_line)
                if processed_seconds is None:
                    continue

                segment_progress = normalize_segment_progress(
                    processed_seconds,
                    segment_duration,
                    expected_start,
                )
                last_segment_progress = max(last_segment_progress, segment_progress)
                print_progress(
                    completed_duration + last_segment_progress,
                    total_duration,
                    start_time,
                    label,
                )

        if process.stderr:
            stderr_text = process.stderr.read()
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        print()
        raise

    if return_code == 0:
        print_progress(completed_duration + segment_duration, total_duration, start_time, label)
    print()
    return return_code, stderr_text

def get_duration(input_file):
    """使用 ffprobe 获取视频总时长（单位：秒）"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_file
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(f"无法读取视频时长：{input_file}") from exc

def reserve_output_basename(input_dir, basename):
    """为本次切割预留一组输出文件名，避免多脚本并发写同名文件"""
    candidates = [basename]
    run_id = f"{basename}_run{os.getpid()}_{int(time.time())}"
    candidates.extend(f"{run_id}_{i}" for i in range(1, 1000))

    for candidate in candidates:
        lock_file = os.path.join(input_dir, f".{candidate}.split.lock")
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RuntimeError(f"无法创建并发锁文件：{lock_file} ({exc})") from exc

        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(f"pid={os.getpid()}\n")
            file.write(f"created_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")

        return candidate, lock_file

    raise RuntimeError("无法为本次切割生成唯一输出文件名")

def release_output_lock(lock_file):
    """释放输出文件名锁；失败时不影响已生成的视频"""
    try:
        if lock_file and os.path.exists(lock_file):
            os.unlink(lock_file)
    except PermissionError:
        print(f"警告: 并发锁文件仍被占用，稍后可手动删除: {lock_file}")
    except OSError as exc:
        print(f"警告: 无法删除并发锁文件 {lock_file}: {exc}")

def split_video(input_file, segment_time=3600, reencode=False):
    """使用 ffmpeg 分割视频，并将输出保存到输入文件所在目录"""
    if not os.path.exists(input_file):
        print(f"❌ 输入文件不存在：{input_file}")
        return
    if segment_time <= 0:
        print("❌ 分段时长必须大于 0 秒")
        return

    input_dir = os.path.dirname(os.path.abspath(input_file))
    basename = os.path.splitext(os.path.basename(input_file))[0]
    try:
        total_duration = get_duration(input_file)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return

    segments = math.ceil(total_duration / segment_time)

    print(f"🎬 原视频: {input_file}")
    print(f"📁 输出目录: {input_dir}")
    print(f"⏱️ 总时长: {format_time(total_duration)} ({int(total_duration)} 秒)")
    print(f"🔪 拟切割为 {segments} 段，每段 {segment_time} 秒")
    print("预计处理时间将根据实时处理速度动态估算")

    try:
        output_basename, lock_file = reserve_output_basename(input_dir, basename)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return

    if output_basename != basename:
        print(f"检测到同名切割任务正在运行，本次输出改用前缀: {output_basename}")

    try:
        start_time = time.monotonic()
        completed_duration = 0.0
        for i in range(segments):
            start = i * segment_time
            actual_duration = min(segment_time, total_duration - start)
            output_file = os.path.join(input_dir, f"{output_basename}_{i+1:02d}.mp4")

            if reencode:
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start),
                    "-i", input_file,
                    "-t", str(actual_duration),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac",
                    output_file
                ]
            else:
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start),
                    "-i", input_file,
                    "-t", str(actual_duration),
                    "-c", "copy",
                    output_file
                ]

            print(f"\n>>> 导出第 {i+1} 段：{output_file}（起始: {start}s，时长: {actual_duration}s）")
            return_code, stderr_text = run_ffmpeg_with_progress(
                cmd,
                actual_duration,
                total_duration,
                completed_duration,
                start_time,
                f"总体进度 (第 {i+1}/{segments} 段)",
                expected_start=start,
            )
            if return_code != 0:
                print("FFmpeg 错误:")
                print(stderr_text)
                return

            completed_duration += actual_duration
    finally:
        release_output_lock(lock_file)

    print("✅ 切割完成！")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python split_video.py <input_file> [segment_seconds] [reencode]")
        print("示例: python split_video.py a.mp4 3600 true")
        sys.exit(1)

    input_path = sys.argv[1]
    seg_time = int(sys.argv[2]) if len(sys.argv) >= 3 else 3600
    reencode_flag = sys.argv[3].lower() == 'true' if len(sys.argv) >= 4 else False

    split_video(input_path, seg_time, reencode_flag)