#!/usr/bin/env python3
"""
D1 パイプライン: 文字起こしの本文を変えずに話者分離を足す。

    python diarize_d1.py <音声> [--speakers 5] [--out out.json]

1. faster-whisper large-v3 で文字起こし（VAD もバッチも使わない素の設定）
2. その本文のまま wav2vec2 で単語の時刻を補正
3. pyannote で話者を分離し、単語へ割り当て

出力は annotator.py がそのまま読める JSON（word_segments に speaker が入る）。
本文は 1 の結果から一切変えない。
"""
import argparse, json, os

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--speakers", type=int, default=None, help="話者数（分かっていれば固定する）")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import whisperx
    from whisperx.diarize import DiarizationPipeline
    from faster_whisper import WhisperModel

    hf_token = os.environ.get("HF_TOKEN") or ""
    tok_file = os.path.expanduser("~/.config/whisperx/hf_token")
    if not hf_token and os.path.exists(tok_file):
        hf_token = open(tok_file).read().strip()
    if not hf_token:
        # トークンが無くても pyannote がキャッシュ済みなら回せる。ネットに出ず
        # キャッシュから読む。これを付けないとゲート付きモデルを取りに行って
        # 401 で落ちる。interview.sh と同じ逃がし方。
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    # --- 1. 素の faster-whisper。transcribe.py と同じ設定にそろえる
    print("1/3 文字起こし (%s)…" % args.model, flush=True)
    model = WhisperModel(args.model, device=args.device,
                         compute_type="float16" if args.device == "cuda" else "float32")
    segments, _info = model.transcribe(args.audio, language="ja", chunk_length=15,
                                       condition_on_previous_text=False)
    segs = [{"start": s.start, "end": s.end, "text": s.text.strip()}
            for s in segments if s.text.strip()]
    print("    %d セグメント" % len(segs), flush=True)

    # 話者分離を足しても本文が変わらないことを、あとで自分で確かめられるように
    # この時点の本文を書き出しておく（最後の .d1.txt と diff すれば分かる）
    base = os.path.splitext(args.out or args.audio)[0]
    with open(base + ".b1.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(x["text"] for x in segs) + "\n")

    # --- 2. wav2vec2 で単語の時刻を補正（本文は変わらない）
    print("2/3 アライメント…", flush=True)
    audio = whisperx.load_audio(args.audio)
    align_model, meta = whisperx.load_align_model(language_code="ja", device=args.device)
    aligned = whisperx.align(segs, align_model, meta, audio, args.device,
                             return_char_alignments=False)

    # --- 3. 話者分離して単語へ割り当て
    print("3/3 話者分離…", flush=True)
    dia = DiarizationPipeline(token=hf_token or None, device=args.device)
    kw = {}
    if args.speakers:
        kw = {"min_speakers": args.speakers, "max_speakers": args.speakers}
    result = whisperx.assign_word_speakers(dia(audio, **kw), aligned)

    out = args.out or (os.path.splitext(args.audio)[0] + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    with open(base + ".d1.txt", "w", encoding="utf-8") as f:
        f.write("\n".join((x.get("text") or "").strip() for x in result["segments"]) + "\n")

    spk = sorted({w.get("speaker") for w in result.get("word_segments", []) if w.get("speaker")})
    print("完了: %s（%d セグメント / 話者 %s）" % (out, len(result["segments"]), spk), flush=True)
    print("本文が変わっていないか: diff %s.b1.txt %s.d1.txt" % (base, base), flush=True)

if __name__ == "__main__":
    main()
