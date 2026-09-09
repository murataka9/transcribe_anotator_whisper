#!/usr/bin/env python3
"""
Apple Silicon の GPU で文字起こしする（MLX 版）。

    python transcribe_mlx.py <音声かディレクトリ>

`transcribe.py` と同じ形式で書き出すので、annotator.py はそのまま読めます。
Mac で使うならこちらです。理由は速度で、実測（M4 / 116秒の音声）はこうでした。

| 方式                                  | 速度      | 30分換算 |
| MLX large-v3（Apple GPU）             | 4.6倍速   | 約6.5分  |
| faster-whisper large-v3 int8（CPU）   | 1.2倍速   | 約24分   |
| faster-whisper large-v3 float32（CPU）| 0.70倍速  | 約43分   |

`transcribe.py` が使う CTranslate2 には Metal 対応が無く、Mac では必ず CPU に
落ちます。既定の float32 だと音声より時間がかかる（0.70倍速）ので、Mac から
UI 越しに叩くには実用になりません。

**量子化版（-4bit / -8bit）は使いません。**日本語の固有名詞が落ちるためで、
turbo を避けているのと同じ理由です。

単語ごとの時刻（word_timestamps）も一緒に出します。これは話者分離のときに
効きます。行の時刻だけで話者を決めると、行の切れ目に隣の話者が漏れ込んで
「迷い」が増えるためです。詳しくは diarize_local.py を見てください。
"""

import argparse
import json
import os

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4")
DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"


def outputs(base):
    """この収録に対して書き出すファイル一式。"""
    return {
        "timecoded": base + "_timecoded.txt",
        "text": base + "_text.txt",
        "words": base + ".words.json",
    }


def transcribe_one(mlx_whisper, path, model, language, prompt, overwrite):
    base, _ = os.path.splitext(path)
    out = outputs(base)

    # 黙って上書きしない。手で直した本文が消えるのが一番痛い。
    exists = [p for p in out.values() if os.path.exists(p)]
    if exists and not overwrite:
        print("× %s : 既に結果があります。消してよければ --overwrite を付けてください"
              % os.path.basename(path))
        for p in exists:
            print("    %s" % os.path.basename(p))
        return False

    print("文字起こし中: %s" % os.path.basename(path), flush=True)
    result = mlx_whisper.transcribe(
        path,
        path_or_hf_repo=model,
        language=language,
        condition_on_previous_text=False,   # transcribe.py と同じ。幻聴の連鎖を防ぐ
        initial_prompt=prompt,
        word_timestamps=True,               # 話者分離で使う（+2割ほどの時間で済む）
    )

    segments = [s for s in result.get("segments", []) if (s.get("text") or "").strip()]
    with open(out["timecoded"], "w", encoding="utf-8") as f_time, \
         open(out["text"], "w", encoding="utf-8") as f_txt:
        for s in segments:
            text = s["text"].strip()
            f_time.write("[%.2fs -> %.2fs] %s\n" % (s["start"], s["end"], text))
            f_txt.write("%s\n" % text)

    # 単語の時刻は話者分離に渡すためだけのもの。人が読むものではない。
    words = [{"start": w["start"], "end": w["end"], "word": w["word"]}
             for s in segments for w in (s.get("words") or [])]
    with open(out["words"], "w", encoding="utf-8") as f:
        json.dump({"segments": [{"start": s["start"], "end": s["end"],
                                 "text": s["text"].strip()} for s in segments],
                   "words": words}, f, ensure_ascii=False)

    print("   %d行 / 単語 %d → %s" % (len(segments), len(words),
                                      os.path.basename(out["timecoded"])), flush=True)
    return True


def main():
    ap = argparse.ArgumentParser(description="Apple GPU で文字起こしする（MLX）")
    ap.add_argument("target", help="音声ファイル、または音声の入ったディレクトリ")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="MLX のモデル（既定: %s）" % DEFAULT_MODEL)
    ap.add_argument("--language", default="ja")
    ap.add_argument("--prompt", default=None,
                    help="固有名詞や句読点のスタイルを誘導する初期プロンプト")
    ap.add_argument("--overwrite", action="store_true",
                    help="既にある結果を上書きする")
    args = ap.parse_args()

    import mlx_whisper

    if os.path.isdir(args.target):
        targets = [os.path.join(args.target, fn) for fn in sorted(os.listdir(args.target))
                   if fn.lower().endswith(AUDIO_EXTENSIONS)]
        if not targets:
            print("音声が見つかりません: %s" % args.target)
            return
    else:
        targets = [args.target]

    done = 0
    for path in targets:
        if transcribe_one(mlx_whisper, path, args.model, args.language,
                          args.prompt, args.overwrite):
            done += 1
    print("完了: %d / %d 件" % (done, len(targets)))


if __name__ == "__main__":
    main()
