#!/usr/bin/env python3
"""
話者分離だけをローカルで回す（文字起こしはしない）。

    python diarize_local.py <音声> [--speakers 5]

出力は annotator.py の「話者を読み込む」がそのまま読める形です。

- 単語の時刻（`<名前>.words.json`）があれば → `<名前>.json`（単語ごとに話者）
- 無ければ                                 → `<名前>.rttm`（話者区間だけ）

**diarize_d1.py との使い分け**

`diarize_d1.py` は文字起こしから3段で回すので whisperx が要り、モデルだけで
5.3GB、実質 CUDA 専用です。こちらは pyannote だけなので **モデルは32MB**、
Apple Silicon の GPU（MPS）で動きます。実測（M4）で 12倍速、30分の音声が
約2分半でした。

    文字起こしは別（transcribe_mlx.py か import_whisper.py）→ こちらで話者を足す

**単語の時刻が効きます。**行の時刻だけで話者を決めると、Whisper の行の
切れ目がずれているぶん隣の話者の声が窓に漏れ込み、票が割れます。同じ30分の
インタビューで比べると、話者の結論は94%一致するのに、**「迷いあり」の印が
23行 → 95行**まで増えました。単語ごとに話者を決めてから行の多数決を取ると、
この水増しが消えます。transcribe_mlx.py が `.words.json` を出すのはこのためです。
"""

import argparse
import os
import sys


def pick_device(want):
    """使える中でいちばん速いものを選ぶ。"""
    import torch
    if want != "auto":
        return want
    if torch.backends.mps.is_available():
        return "mps"          # Apple Silicon。CPU の約6倍
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_pipeline(device):
    """pyannote のパイプラインを読む。トークンが無ければキャッシュから読む。

    community-1 は単一のチェックポイントなので、一度取得してあれば
    HF_HUB_OFFLINE でゲートを通らずに読めます（3系のように segmentation と
    embedding を別リポジトリから引く必要がありません）。
    """
    import torch
    from pyannote.audio import Pipeline

    token = os.environ.get("HF_TOKEN") or ""
    token_file = os.path.expanduser("~/.config/whisperx/hf_token")
    if not token and os.path.exists(token_file):
        token = open(token_file).read().strip()
    if not token:
        # トークンが無くてもキャッシュ済みなら回せる。ネットに出ずキャッシュから読む。
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    # MPS に無い演算は CPU に落とす
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    try:
        pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1",
                                        token=token or None)
    except Exception as e:
        print("話者分離モデルを読めませんでした: %s" % e, file=sys.stderr)
        print("初回は HuggingFace でゲートに同意し、HF_TOKEN を渡してください"
              "（一度取得すればキャッシュから読めます）", file=sys.stderr)
        raise SystemExit(1)
    return pipe.to(torch.device(device))


def assign_words(words, turns):
    """単語ごとに、いちばん長く重なった話者を割り当てる。"""
    import bisect
    turns = sorted(turns, key=lambda t: t[0])
    starts = [t[0] for t in turns]
    longest = max((b - a) for a, b, _ in turns)
    out = []
    for w in words:
        a0, a1 = w["start"], w["end"]
        overlap = {}
        i = bisect.bisect_left(starts, a0 - longest)
        while i < len(turns) and turns[i][0] < a1:
            b0, b1, spk = turns[i]
            d = min(b1, a1) - max(b0, a0)
            if d > 0:
                overlap[spk] = overlap.get(spk, 0.0) + d
            i += 1
        row = {"start": a0, "end": a1, "word": w["word"]}
        if overlap:
            row["speaker"] = max(overlap.items(), key=lambda kv: kv[1])[0]
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser(description="ローカルで話者分離だけを行う")
    ap.add_argument("audio")
    ap.add_argument("--speakers", type=int, default=None,
                    help="話者の人数。分かっているなら指定したほうが精度が出る（既定: 自動推定）")
    ap.add_argument("--device", default="auto", choices=["auto", "mps", "cuda", "cpu"])
    ap.add_argument("--words", default=None,
                    help="単語の時刻の JSON（既定: <音声名>.words.json があれば使う）")
    ap.add_argument("--overwrite", action="store_true", help="既にある結果を上書きする")
    args = ap.parse_args()

    import json
    import time

    base = os.path.splitext(args.audio)[0]
    words_path = args.words or (base + ".words.json")
    has_words = os.path.exists(words_path)
    out_path = base + (".json" if has_words else ".rttm")

    if os.path.exists(out_path) and not args.overwrite:
        print("× %s が既にあります。消してよければ --overwrite を付けてください"
              % os.path.basename(out_path))
        raise SystemExit(1)

    device = pick_device(args.device)
    print("話者分離中（%s / 話者 %s）…"
          % (device, args.speakers if args.speakers else "自動"), flush=True)
    pipe = load_pipeline(device)

    t0 = time.time()
    kw = {"num_speakers": args.speakers} if args.speakers else {}
    result = pipe(args.audio, **kw)
    elapsed = time.time() - t0

    # pyannote 4系は DiarizeOutput を返す。3系は Annotation がそのまま返る。
    annotation = getattr(result, "speaker_diarization", result)
    turns = [(seg.start, seg.end, label)
             for seg, _, label in annotation.itertracks(yield_label=True)]
    speakers = sorted({label for *_, label in turns})

    if has_words:
        data = json.load(open(words_path, encoding="utf-8"))
        word_segments = assign_words(data.get("words", []), turns)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"segments": data.get("segments", []),
                       "word_segments": word_segments}, f, ensure_ascii=False)
        unassigned = sum(1 for w in word_segments if "speaker" not in w)
        detail = "単語 %d（話者なし %d）" % (len(word_segments), unassigned)
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            annotation.write_rttm(f)
        detail = "話者区間 %d" % len(turns)
        print("   ヒント: <名前>.words.json があれば単語ごとに話者を付けられます"
              "（迷い印が減ります）", flush=True)

    print("完了: %s（%.1f秒 / 話者 %s / %s）"
          % (os.path.basename(out_path), elapsed, speakers, detail), flush=True)


if __name__ == "__main__":
    main()
