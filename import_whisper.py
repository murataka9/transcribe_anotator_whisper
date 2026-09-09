#!/usr/bin/env python3
"""
OpenAI Whisper / WhisperX の出力を annotator.py が読める形に取り込む。

    python import_whisper.py <ソースdir> [--dest recordings] [--copy]

ソースdir 直下の音声と、配下の JSON/SRT を「長さ」で突き合わせ、dest に
    <音声名>_timecoded.txt   … [開始s -> 終了s] テキスト
    <音声名><拡張子>          … 音声（既定はシンボリックリンク／--copy で実体コピー）
を作る。録音アプリ由来でトランスクリプトのファイル名が音声と食い違っていても、
末尾時刻と音声長のいちばん近いものを自動で組にする。
"""

import argparse
import json
import os
import re
import shutil
import subprocess

AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4")
SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def duration(path):
    """ffprobe で秒数を得る。取れなければ None。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


def read_json(path):
    """Whisper / WhisperX の JSON → [(start, end, text), ...]"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segments = data["segments"] if isinstance(data, dict) else data
    return [
        (float(s["start"]), float(s["end"]), s["text"].strip())
        for s in segments
        if s.get("text", "").strip()
    ]


def read_srt(path):
    """SRT → [(start, end, text), ...]"""
    with open(path, encoding="utf-8-sig") as f:
        blocks = re.split(r"\n\s*\n", f.read().strip())
    out = []
    for block in blocks:
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = SRT_TIME.search(line)
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
            end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
            text = " ".join(x.strip() for x in lines[i + 1:]).strip()
            if text:
                out.append((start, end, text))
            break
    return out


def load_transcripts(src):
    """src 配下の JSON/SRT を全部読む。同名は JSON を優先する。"""
    found = {}
    for root, _dirs, files in os.walk(src):
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".json", ".srt"):
                continue
            path = os.path.join(root, fn)
            try:
                segs = read_json(path) if ext == ".json" else read_srt(path)
            except Exception as e:
                print("  読めないので飛ばす: %s (%s)" % (fn, e))
                continue
            if not segs:
                continue
            key = os.path.join(root, os.path.splitext(fn)[0])
            # JSON を優先（同じ内容なら .json のほうが情報が多い）
            if key in found and found[key]["ext"] == ".json":
                continue
            found[key] = {"path": path, "ext": ext, "segs": segs,
                          "end": segs[-1][1]}
    return list(found.values())


def write_timecoded(segs, dest_path):
    with open(dest_path, "w", encoding="utf-8") as f:
        for start, end, text in segs:
            f.write("[%.2fs -> %.2fs] %s\n" % (start, end, text))


def link_audio(src, dest, copy):
    if os.path.lexists(dest):
        os.remove(dest)
    if copy:
        shutil.copy2(src, dest)
    else:
        os.symlink(os.path.abspath(src), dest)


def main():
    ap = argparse.ArgumentParser(
        description="Whisper の出力を annotator.py 用に取り込む")
    ap.add_argument("src", help="音声とトランスクリプトのあるディレクトリ")
    ap.add_argument("--dest", default="recordings", help="取り込み先（既定: recordings）")
    ap.add_argument("--copy", action="store_true",
                    help="音声をシンボリックリンクではなく実体でコピーする")
    ap.add_argument("--tolerance", type=float, default=60.0,
                    help="音声長との許容差（秒。既定 60）")
    args = ap.parse_args()

    audios = sorted(
        os.path.join(args.src, fn)
        for fn in os.listdir(args.src)
        if fn.lower().endswith(AUDIO_EXT)
    )
    if not audios:
        print("音声が見つかりません: %s" % args.src)
        return

    transcripts = load_transcripts(args.src)
    if not transcripts:
        print("JSON/SRT が見つかりません: %s" % args.src)
        return

    os.makedirs(args.dest, exist_ok=True)
    unused = list(transcripts)

    for audio in audios:
        name, ext = os.path.splitext(os.path.basename(audio))
        dur = duration(audio)
        if dur is None:
            print("× %s : ffprobe で長さが取れないので飛ばす" % name)
            continue
        # 末尾時刻がいちばん近いトランスクリプトを選ぶ
        best = min(unused, key=lambda t: abs(t["end"] - dur), default=None)
        if best is None or abs(best["end"] - dur) > args.tolerance:
            gap = "" if best is None else "（最も近いもので %.0f秒差）" % abs(best["end"] - dur)
            print("× %s : 長さの合うトランスクリプトが無い%s" % (name, gap))
            continue
        unused.remove(best)

        write_timecoded(best["segs"], os.path.join(args.dest, name + "_timecoded.txt"))
        link_audio(audio, os.path.join(args.dest, name + ext), args.copy)
        print("○ %s\n    ← %s（%d行 / 音声 %.0f秒 vs 文字起こし %.0f秒）"
              % (name, os.path.basename(best["path"]), len(best["segs"]), dur, best["end"]))

    for t in unused:
        print("- 使わなかった: %s" % os.path.basename(t["path"]))


if __name__ == "__main__":
    main()
