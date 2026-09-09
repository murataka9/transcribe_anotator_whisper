#!/usr/bin/env python3
"""
トランスクリプト話者アノテーター

faster-whisper で生成した `[開始s -> 終了s] テキスト` 形式のタイムコード付き
トランスクリプトと音声を並べ、ブラウザ上で
  - 発話者ロールのラベル付け（任意個数のラベル）
  - 誤字の修正・セグメントの分割/結合
  - 置換辞書（「誤 → 正」）による一括置換
を行い、MAXQDA 等に読み込めるテキストへ書き出すためのローカルツール。

追加パッケージ不要（Python 標準ライブラリのみ）。
    python annotator.py            # recordings/ を対象に http://127.0.0.1:8000 で起動
    python annotator.py --dir some_dir --port 8000

置換辞書はリポジトリ直下の replacements.json（--replacements で変更可）。

状態は各収録ごとに recordings/<name>.annot.json に自動保存されるため、
途中でブラウザやサーバーが落ちても再起動すれば続きから再開できる。
書き出しは <name>_annotated.txt へ（元の *_timecoded.txt は書き換えない）。
"""

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ここで import するのは標準ライブラリだけ。文字起こしや話者分離は別プロセスに
# 投げるので、**アノテーターだけを使う人は何もインストールしなくていい**。
# その前提は崩さないこと（README の「標準ライブラリのみで動く」はこの意味）。

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.join(BASE_DIR, "webui")
# 置換辞書はリポジトリ直下に置く（recordings/ は .gitignore なので共有できないため）
DEFAULT_REPLACEMENTS = os.path.join(BASE_DIR, "replacements.json")

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4")
DEFAULT_ROLES = ["インタビュアー", "インタビュイー"]

# "[12.34s -> 56.78s] テキスト" にマッチ
LINE_RE = re.compile(r"^\s*\[\s*([0-9.]+)s\s*->\s*([0-9.]+)s\s*\]\s?(.*)$")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


def audio_mime(path):
    return MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def probe_duration(path):
    """音声の秒数。ffprobe が無ければ None を返すだけで、動作には影響しない。"""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        return round(float(out), 2)
    except Exception:
        return None


def capabilities():
    """この環境で何ができるかを調べる。

    アノテーターは標準ライブラリだけで動くので、文字起こしも話者分離も
    「無ければ無いなりに」動かす。UI 側はここを見て、使えない機能を
    押せなくする（押してから失敗するのが一番たちが悪い）。
    """
    def importable(mod):
        try:
            subprocess.run([sys.executable, "-c", "import " + mod],
                           capture_output=True, timeout=60, check=True)
            return True
        except Exception:
            return False

    caps = {
        "transcribe_mlx": (sys.platform == "darwin"
                           and os.path.exists(os.path.join(BASE_DIR, "transcribe_mlx.py"))
                           and importable("mlx_whisper")),
        "diarize": (os.path.exists(os.path.join(BASE_DIR, "diarize_local.py"))
                    and importable("pyannote.audio")),
        "ffprobe": bool(shutil.which("ffprobe")),
    }
    return caps


class Store:
    """recordings ディレクトリの走査と、アノテーション JSON の読み書き。"""

    def __init__(self, data_dir, replacements_path=None):
        self.data_dir = os.path.abspath(data_dir)
        self.replacements_path = os.path.abspath(replacements_path or DEFAULT_REPLACEMENTS)

    # --- 収録一覧 -----------------------------------------------------
    def list_projects(self):
        """収録の一覧を返す。

        文字起こし済み（*_timecoded.txt がある）ものに加えて、**音声だけ置かれて
        まだ文字起こししていないもの**も返す。ここに出てこないと、UI から
        「これを文字起こしする」を選べないため。
        """
        projects = []
        try:
            names = sorted(os.listdir(self.data_dir))
        except FileNotFoundError:
            return projects
        found = []
        seen = set()
        for fn in names:
            if fn.endswith("_timecoded.txt"):
                name = fn[: -len("_timecoded.txt")]
            elif fn.lower().endswith(AUDIO_EXTENSIONS):
                name = os.path.splitext(fn)[0]
            else:
                continue
            if name not in seen:
                seen.add(name)
                found.append(name)
        for name in sorted(found):
            audio = self._find_audio(name)
            projects.append({
                "name": name,
                "audio": bool(audio),
                "transcribed": os.path.exists(self._timecoded_path(name)),
                "annotated": os.path.exists(self._annot_path(name)),
                "diarization": bool(self.find_diarization(name)),
            })
        return projects

    def _find_audio(self, name):
        for ext in AUDIO_EXTENSIONS:
            p = os.path.join(self.data_dir, name + ext)
            if os.path.exists(p):
                return p
        return None

    def _timecoded_path(self, name):
        return os.path.join(self.data_dir, name + "_timecoded.txt")

    def _annot_path(self, name):
        return os.path.join(self.data_dir, name + ".annot.json")

    def _export_path(self, name):
        return os.path.join(self.data_dir, name + "_annotated.txt")

    def _safe(self, name):
        # ディレクトリトラバーサル防止：基本ファイル名以外は拒否
        return name and ("/" not in name) and ("\\" not in name) and (".." not in name)

    # --- ファイルを追加 ----------------------------------------------
    # ブラウザは選んだファイルの実際のパスをページに渡さない（fakepath になる）。
    # そのため「どのファイルか」はサーバー側で一覧を出して選んでもらう。
    # 中身を HTTP で送らないので multipart の解析も不要になる。

    def list_sources(self, dirpath):
        """取り込み元ディレクトリの音声を一覧する。"""
        dirpath = os.path.abspath(os.path.expanduser(dirpath))
        if not os.path.isdir(dirpath):
            return {"error": "not_a_dir", "dir": dirpath}
        rows = []
        for fn in sorted(os.listdir(dirpath)):
            if not fn.lower().endswith(AUDIO_EXTENSIONS):
                continue
            path = os.path.join(dirpath, fn)
            name = os.path.splitext(fn)[0]
            rows.append({
                "file": fn,
                "size": os.path.getsize(path),
                "duration": probe_duration(path),
                # 同名が既にあるかは、押す前に見せておきたい
                "exists": bool(self._find_audio(name)),
            })
        return {"dir": dirpath, "files": rows}

    def add_source(self, dirpath, filename, mode="ask"):
        """取り込み元の音声を recordings/ に実体コピーする。

        mode は "ask"（既定・衝突したら何もせず知らせる）/ "overwrite" /
        "keep_both"（連番を付けて両方残す）。黙って上書きしないための作り。
        """
        if not self._safe(filename):
            return {"error": "bad_name"}
        src = os.path.join(os.path.abspath(os.path.expanduser(dirpath)), filename)
        if not os.path.isfile(src):
            return {"error": "not_found"}

        stem, ext = os.path.splitext(filename)
        dest_name = stem
        if self._find_audio(stem):
            if mode == "ask":
                return {"error": "exists", "name": stem,
                        "artifacts": self.artifacts(stem)}
            if mode == "keep_both":
                i = 2
                while self._find_audio("%s-%d" % (stem, i)):
                    i += 1
                dest_name = "%s-%d" % (stem, i)
            elif mode != "overwrite":
                return {"error": "bad_mode"}

        dest = os.path.join(self.data_dir, dest_name + ext)
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = dest + ".part"
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
        os.replace(tmp, dest)   # 途中で落ちても半端なファイルが残らない
        return {"name": dest_name, "file": os.path.basename(dest),
                "size": os.path.getsize(dest)}

    def artifacts(self, name):
        """この収録について既にある成果物。上書き確認で見せる。"""
        rows = []
        for label, path in (
            ("文字起こし", self._timecoded_path(name)),
            ("テキスト", os.path.join(self.data_dir, name + "_text.txt")),
            ("単語の時刻", os.path.join(self.data_dir, name + ".words.json")),
            ("話者分離", self.find_diarization(name) or ""),
            ("アノテーション", self._annot_path(name)),
        ):
            if path and os.path.exists(path):
                rows.append({"label": label, "file": os.path.basename(path)})
        return rows

    # --- トランスクリプト解析 ----------------------------------------
    def parse_timecoded(self, name):
        segments = []
        with open(self._timecoded_path(name), encoding="utf-8") as f:
            for i, raw in enumerate(f):
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                m = LINE_RE.match(line)
                if m:
                    start, end, text = float(m.group(1)), float(m.group(2)), m.group(3)
                else:
                    # タイムコードの無い行も一応取り込む
                    start = end = 0.0
                    text = line.strip()
                segments.append(
                    {
                        "id": i,
                        "start": start,
                        "end": end,
                        "text": text,
                        "original": text,
                        "role": None,
                    }
                )
        return segments

    # --- プロジェクト状態 --------------------------------------------
    def load_project(self, name):
        if not self._safe(name):
            return None
        annot = self._annot_path(name)
        if os.path.exists(annot):
            with open(annot, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("roles", list(DEFAULT_ROLES))
            data.setdefault("options", {"merge": True, "timecodes": False})
            data.setdefault("origins", {})
        else:
            audio_only = self._find_audio(name) and not os.path.exists(self._timecoded_path(name))
            if not os.path.exists(self._timecoded_path(name)) and not audio_only:
                return None
            data = {
                "name": name,
                "roles": list(DEFAULT_ROLES),
                "origins": {},
                "options": {"merge": True, "timecodes": False},
                # まだ文字起こししていない収録は空で開く。UI 側で「文字起こし」を
                # 促すため、存在しないものとして弾かない。
                "segments": [] if audio_only else self.parse_timecoded(name),
            }
        audio = self._find_audio(name)
        data["name"] = name
        data["has_audio"] = bool(audio)
        data["transcribed"] = os.path.exists(self._timecoded_path(name))
        # 所要時間の目安に使う。ffprobe が無ければ None（目安が出ないだけ）。
        data["duration"] = probe_duration(audio) if audio else None
        data["has_words"] = os.path.exists(os.path.join(self.data_dir, name + ".words.json"))
        data["diarization"] = bool(self.find_diarization(name))
        return data

    def reset_project(self, name):
        """トランスクリプトから作り直す（ラベルは失われる）。"""
        if not self._safe(name) or not os.path.exists(self._timecoded_path(name)):
            return None
        data = {
            "name": name,
            "roles": list(DEFAULT_ROLES),
            "origins": {},
            "options": {"merge": True, "timecodes": False},
            "segments": self.parse_timecoded(name),
        }
        self.save_project(name, data)
        return self.load_project(name)

    def save_project(self, name, data):
        if not self._safe(name):
            return False
        path = self._annot_path(name)
        tmp = path + ".tmp"
        payload = {
            "name": name,
            "roles": data.get("roles", list(DEFAULT_ROLES)),
            # origins は「話者分離のラベル → 人に付けた名前」の対応。
            # 名前を付けたあとも、どの SPEAKER_xx だったかを追えるように残す。
            "origins": data.get("origins", {}),
            "options": data.get("options", {"merge": True, "timecodes": False}),
            "segments": data.get("segments", []),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)  # アトミック置換
        return True

    # --- 話者分離の取り込み ------------------------------------------
    # WhisperX の JSON か RTTM から「誰がいつ喋ったか」を読み、既存の行に
    # 話者を割り当てる。日本語は分かち書きしないので WhisperX の words[] は
    # 1文字ずつになり、文字単位の話者ラベルはバタついて使えない。そのため
    # 行ごとに「重なった時間で重み付けした多数決」で1人に決める。

    DIARIZATION_SUFFIXES = (".diarization.json", ".json", ".rttm")

    def find_diarization(self, name):
        """収録と同じ場所にある話者分離ファイルを探す。"""
        for suffix in self.DIARIZATION_SUFFIXES:
            path = os.path.join(self.data_dir, name + suffix)
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def parse_rttm(path):
        """RTTM → [(start, end, speaker), ...]"""
        spans = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                # SPEAKER <file> <ch> <start> <dur> <NA> <NA> <speaker> <NA> <NA>
                if len(parts) < 8 or parts[0].upper() != "SPEAKER":
                    continue
                try:
                    start, dur = float(parts[3]), float(parts[4])
                except ValueError:
                    continue
                spans.append((start, start + dur, parts[7]))
        return spans

    @staticmethod
    def parse_whisperx(path):
        """WhisperX JSON → [(start, end, speaker), ...]

        word_segments（1文字ずつ）を優先し、無ければ segments を使う。
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return []
        rows = data.get("word_segments") or []
        if not any(r.get("speaker") for r in rows if isinstance(r, dict)):
            rows = data.get("segments") or []
        spans = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            spk = r.get("speaker")
            start, end = r.get("start"), r.get("end")
            if spk is None or start is None or end is None:
                continue  # アライメントが付かなかった語は時刻が無い
            try:
                spans.append((float(start), float(end), str(spk)))
            except (TypeError, ValueError):
                continue
        return spans

    def load_diarization(self, path):
        if path.lower().endswith(".rttm"):
            return self.parse_rttm(path)
        return self.parse_whisperx(path)

    def apply_speakers(self, name, mapping=None, margin=0.6):
        """話者分離ファイルを読み、各行に話者を割り当てる。

        行 [start, end] に対し、話者ごとの「重なった時間」を合計し、最大の
        話者を採る。最大が2位の margin 倍を超えない行は迷いありとして印を
        付け、割り当ては行うが件数を返して人手確認を促す。
        mapping は {"SPEAKER_00": "インタビュアー"} のような読み替え。
        """
        data = self.load_project(name)
        if data is None:
            return None
        path = self.find_diarization(name)
        if not path:
            return {"error": "no_file"}
        # 上書きされる手作業がどれだけあるか、呼び出し側に知らせる
        had_roles = sum(1 for s in data["segments"] if s.get("role"))
        spans = self.load_diarization(path)
        if not spans:
            return {"error": "empty", "path": path}

        mapping = mapping or {}
        speakers = sorted({spk for _, _, spk in spans})   # SPEAKER_00, 01, ... の順

        # 前回この収録で SPEAKER_xx に付けた名前を引き継ぐ。引き継がないと、
        # 読み込み直すたびに手でやった照合が SPEAKER_00 に戻ってしまう。
        # 優先順位は「今回の指定 → 前回付けた名前 → 元のラベル」。
        previous = data.get("origins") or {}
        resolved = {spk: (mapping.get(spk) or previous.get(spk) or spk) for spk in speakers}

        spans.sort(key=lambda x: x[0])
        starts = [sp[0] for sp in spans]
        # 区間は重なりうるので、開始位置だけでは走査の始点を決められない。
        # 最長の区間ぶんだけ手前から見れば、重なる区間を取りこぼさない。
        max_dur = max((b - a) for a, b, _ in spans)

        assigned, unclear, unmatched = 0, 0, 0
        for seg in data["segments"]:
            s0, s1 = float(seg.get("start") or 0), float(seg.get("end") or 0)
            if s1 <= s0:
                unmatched += 1
                continue
            # 開始が行末より後になる位置まで走査すれば十分
            overlap = {}
            i = bisect.bisect_left(starts, s0 - max_dur)
            while i < len(spans) and spans[i][0] < s1:
                a, b, spk = spans[i]
                dur = min(b, s1) - max(a, s0)
                if dur > 0:
                    overlap[spk] = overlap.get(spk, 0.0) + dur
                i += 1
            if not overlap:
                seg["role"] = None
                seg.pop("unclear", None)
                unmatched += 1
                continue
            ranked = sorted(overlap.items(), key=lambda kv: -kv[1])
            top, top_dur = ranked[0]
            seg["role"] = resolved[top]
            assigned += 1
            if len(ranked) > 1 and ranked[1][1] > top_dur * margin:
                seg["unclear"] = True   # 2位と僅差。人手で確認したい行
                unclear += 1
            else:
                seg.pop("unclear", None)

        # 全行を振り直すので、既存のロールは残さず話者分離の結果で置き換える。
        # 残すと使われない「インタビュアー/インタビュイー」が並んで邪魔になる。
        data["roles"] = [resolved[spk] for spk in speakers]
        data["origins"] = resolved
        self.save_project(name, data)
        return {"path": os.path.basename(path), "speakers": speakers,
                "assigned": assigned, "unclear": unclear, "unmatched": unmatched,
                "kept_names": {k: v for k, v in resolved.items() if k != v},
                "had_roles": had_roles, "state": self.load_project(name)}

    # --- 置換辞書 ----------------------------------------------------
    # 「誤 → 正」の決定的な置換。Whisper のモデルを上げても残る同音語・
    # 言い間違い・漢字の揺れを潰すためのもの。件数の上限は無い。

    def load_replacements(self):
        """[{"from": ..., "to": ...}, ...] を返す。壊れていても落とさない。"""
        try:
            with open(self.replacements_path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        items = data.get("replacements") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        out, seen = [], set()
        for it in items:
            if not isinstance(it, dict):
                continue
            src = (it.get("from") or "").strip()
            dst = it.get("to") or ""
            if src and src not in seen:  # 手で編集された辞書に重複があっても無視する
                seen.add(src)
                out.append({"from": src, "to": dst})
        return out

    def save_replacements(self, items):
        seen, clean = set(), []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            src = (it.get("from") or "").strip()
            dst = it.get("to") or ""
            if not src or src in seen:
                continue  # 空と重複は捨てる（同じ誤りに2つの正解を持たせない）
            seen.add(src)
            clean.append({"from": src, "to": dst})
        tmp = self.replacements_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"replacements": clean}, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, self.replacements_path)  # アトミック置換
        return clean

    def apply_replacements(self, name):
        """収録の全行に置換辞書を当て、置換後の状態を保存して結果を返す。"""
        data = self.load_project(name)
        if data is None:
            return None
        rules = self.load_replacements()
        if not rules:
            return {"applied": 0, "segments": 0, "details": [], "state": data}

        # 長い語を先に並べる。正規表現の | は左優先なので、これで最長一致になり、
        # 短い語が長い語の一部を先に食う事故を防ぐ。
        rules = sorted(rules, key=lambda r: len(r["from"]), reverse=True)
        mapping = {r["from"]: r["to"] for r in rules}
        pattern = re.compile("|".join(re.escape(r["from"]) for r in rules))
        counts = {r["from"]: 0 for r in rules}

        def sub(m):
            # 単一パスで置換する。置換した結果を別の規則が再び置換する
            # （A→B したあと B→C が走る）連鎖を避けるため。
            counts[m.group(0)] += 1
            return mapping[m.group(0)]

        touched = 0
        for seg in data["segments"]:
            before = seg.get("text") or ""
            after = pattern.sub(sub, before)
            if after != before:
                seg["text"] = after
                touched += 1
        if touched:
            self.save_project(name, data)
        details = [{"from": k, "count": v} for k, v in counts.items() if v]
        details.sort(key=lambda d: -d["count"])
        return {"applied": sum(counts.values()), "segments": touched,
                "details": details, "state": self.load_project(name)}

    # --- 書き出し ----------------------------------------------------
    def export_project(self, name):
        data = self.load_project(name)
        if data is None:
            return None
        opts = data.get("options", {})
        merge = opts.get("merge", True)
        timecodes = opts.get("timecodes", False)
        segs = [s for s in data["segments"] if (s.get("text") or "").strip() != ""]

        def label(role):
            return role if role else "未設定"

        blocks = []  # (role, text, start, end)
        for s in segs:
            role = s.get("role")
            text = s["text"].strip()
            if merge and blocks and blocks[-1][0] == role:
                prev = blocks[-1]
                blocks[-1] = (role, prev[1] + " " + text, prev[2], s["end"])
            else:
                blocks.append((role, text, s["start"], s["end"]))

        lines = []
        for role, text, start, end in blocks:
            prefix = "(%s) " % label(role)
            if timecodes:
                prefix = "[%.1f-%.1f] " % (start, end) + prefix
            lines.append(prefix + text)

        out = self._export_path(name)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, out)
        unlabeled = sum(1 for s in segs if not s.get("role"))
        return {"path": out, "lines": len(lines), "unlabeled": unlabeled}


class JobRunner:
    """文字起こし・話者分離を別プロセスで回し、進捗を持っておく。

    状態をサーバー側に置くので、**ページを閉じて開き直しても進捗に戻れる**。
    GPU を取り合わないよう、走らせるのは一度に1本だけ。
    """

    # 実測値（M4 / 日本語）。残り時間の目安を出すのに使う。
    RATE = {"transcribe": 4.6, "diarize": 12.0}

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.job = None

    def snapshot(self):
        with self.lock:
            if not self.job:
                return None
            job = dict(self.job)
            if job["status"] == "running":
                # 文字起こしは途中で何も出力しないので、経過はここで数える
                job["elapsed"] = round(time.time() - job["started"])
            return job

    def busy(self):
        with self.lock:
            return bool(self.job and self.job["status"] == "running")

    def start(self, name, do_transcribe, do_diarize, speakers, model):
        audio = self.store._find_audio(name)
        if not audio:
            return {"error": "no_audio"}
        stages = []
        duration = probe_duration(audio)
        if do_transcribe:
            stages.append({"key": "transcribe", "label": "文字起こし", "status": "waiting",
                           "estimate": round(duration / self.RATE["transcribe"]) if duration else None})
        if do_diarize:
            stages.append({"key": "diarize", "label": "話者分離", "status": "waiting",
                           "estimate": round(duration / self.RATE["diarize"]) if duration else None})
        if not stages:
            return {"error": "nothing_to_do"}

        with self.lock:
            if self.job and self.job["status"] == "running":
                return {"error": "busy", "running": self.job["name"]}
            self.job = {
                "name": name, "status": "running", "stages": stages,
                "speakers": speakers, "model": model, "started": time.time(),
                "elapsed": 0, "current": stages[0]["key"], "log": [], "error": None,
            }
        threading.Thread(target=self._run, args=(name, audio), daemon=True).start()
        return self.snapshot()

    def _run(self, name, audio):
        try:
            for stage in list(self.job["stages"]):
                self._set(current=stage["key"])
                self._stage(stage["key"], "running")
                cmd = self._command(stage["key"], audio)
                code = self._spawn(cmd)
                if code != 0:
                    self._stage(stage["key"], "failed")
                    self._set(status="failed",
                              error="%s に失敗しました（終了コード %d）" % (stage["label"], code))
                    return
                self._stage(stage["key"], "done")
            self._set(status="done")
        except Exception as e:                        # 予期しない失敗も画面に出す
            self._set(status="failed", error=str(e))

    def _command(self, key, audio):
        python = sys.executable
        if key == "transcribe":
            cmd = [python, os.path.join(BASE_DIR, "transcribe_mlx.py"), audio, "--overwrite"]
            if self.job.get("model"):
                cmd += ["--model", self.job["model"]]
            return cmd
        cmd = [python, os.path.join(BASE_DIR, "diarize_local.py"), audio, "--overwrite"]
        if self.job.get("speakers"):
            cmd += ["--speakers", str(self.job["speakers"])]
        return cmd

    def _spawn(self, cmd):
        proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if "\r" in line:
                line = line.split("\r")[-1].strip()   # 進捗バーは最後の状態だけ
            # ライブラリの警告は画面に出さない。pyannote が毎回吐く
            # RuntimeWarning などで、進捗の表示が埋まってしまうため。
            noise = ("Warning:" in line or line.startswith(("Fetching ", "  ", "\t"))
                     or "warnings.warn" in line or line.startswith("/"))
            if line and not noise:
                with self.lock:
                    self.job["log"] = (self.job["log"] + [line])[-40:]
                    self.job["elapsed"] = round(time.time() - self.job["started"])
        return proc.wait()

    def _set(self, **kw):
        with self.lock:
            self.job.update(kw)
            self.job["elapsed"] = round(time.time() - self.job["started"])

    def _stage(self, key, status):
        with self.lock:
            for s in self.job["stages"]:
                if s["key"] == key:
                    s["status"] = status
            self.job["elapsed"] = round(time.time() - self.job["started"])


class Handler(BaseHTTPRequestHandler):
    store = None       # サーバー起動時に注入
    jobs = None        # JobRunner
    caps = {}          # この環境でできること
    source_dir = ""    # 「ファイルを追加」で最初に開くディレクトリ
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静かに

    def handle(self):
        # ブラウザが keep-alive / 音声シークで接続を切るのは正常。ログを汚さない。
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    # --- 送信ヘルパ --------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return {}

    # --- 静的ファイル ------------------------------------------------
    def _serve_static(self, rel):
        if rel == "" or rel == "/":
            rel = "index.html"
        rel = rel.lstrip("/")
        path = os.path.normpath(os.path.join(WEBUI_DIR, rel))
        if not path.startswith(WEBUI_DIR) or not os.path.isfile(path):
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        ext = os.path.splitext(path)[1].lower()
        self._send_bytes(body, MIME.get(ext, "application/octet-stream"))

    # --- 音声（Range 対応）------------------------------------------
    def _serve_audio(self, name):
        if not self.store._safe(name):
            self._send_bytes(b"Bad name", "text/plain", 400)
            return
        path = self.store._find_audio(name)
        if not path:
            self._send_bytes(b"No audio", "text/plain", 404)
            return
        size = os.path.getsize(path)
        ctype = audio_mime(path)
        range_header = self.headers.get("Range")

        if range_header is None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    self._copy(f, size)
            return

        # "bytes=start-end" を解釈（open-ended も許容）
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if m:
            g1, g2 = m.group(1), m.group(2)
            if g1 == "" and g2 != "":  # 末尾 N バイト
                length = min(int(g2), size)
                start = size - length
                end = size - 1
            else:
                if g1 != "":
                    start = int(g1)
                if g2 != "":
                    end = int(g2)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
        end = min(end, size - 1)
        length = end - start + 1

        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if self.command != "HEAD":
            with open(path, "rb") as f:
                f.seek(start)
                self._copy(f, length)

    def _copy(self, f, remaining):
        chunk = 64 * 1024
        try:
            while remaining > 0:
                buf = f.read(min(chunk, remaining))
                if not buf:
                    break
                self.wfile.write(buf)
                remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass  # ブラウザがシークで接続を切るのは正常

    # --- ルーティング ------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/list":
            self._send_json({"projects": self.store.list_projects()})
        elif path == "/api/replacements":
            self._send_json({"replacements": self.store.load_replacements(),
                             "path": self.store.replacements_path})
        elif path == "/api/diarization":
            name = (qs.get("name") or [""])[0]
            found = self.store.find_diarization(name) if self.store._safe(name) else None
            if not found:
                self._send_json({"found": False})
            else:
                spans = self.store.load_diarization(found)
                self._send_json({"found": True, "file": os.path.basename(found),
                                 "spans": len(spans),
                                 "speakers": sorted({sp[2] for sp in spans})})
        elif path == "/api/project":
            name = (qs.get("name") or [""])[0]
            data = self.store.load_project(name)
            if data is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(data)
        elif path == "/api/capabilities":
            self._send_json({"capabilities": self.caps, "source_dir": self.source_dir})
        elif path == "/api/sources":
            d = (qs.get("dir") or [self.source_dir])[0]
            self._send_json(self.store.list_sources(d))
        elif path == "/api/artifacts":
            name = (qs.get("name") or [""])[0]
            if not self.store._safe(name):
                self._send_json({"error": "bad_name"}, 400)
            else:
                self._send_json({"name": name, "artifacts": self.store.artifacts(name)})
        elif path == "/api/job":
            self._send_json({"job": self.jobs.snapshot()})
        elif path == "/audio":
            self._serve_audio((qs.get("name") or [""])[0])
        else:
            self._serve_static(path)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/audio":
            qs = urllib.parse.parse_qs(parsed.query)
            self._serve_audio((qs.get("name") or [""])[0])
        else:
            self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        name = (qs.get("name") or [""])[0]

        if path == "/api/save":
            data = self._read_body()
            ok = self.store.save_project(name, data)
            self._send_json({"ok": ok})
        elif path == "/api/reset":
            data = self.store.reset_project(name)
            if data is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(data)
        elif path == "/api/replacements":
            body = self._read_body()
            items = body.get("replacements") if isinstance(body, dict) else body
            saved = self.store.save_replacements(items)
            self._send_json({"ok": True, "replacements": saved})
        elif path == "/api/apply-speakers":
            body = self._read_body()
            mapping = body.get("mapping") if isinstance(body, dict) else None
            result = self.store.apply_speakers(name, mapping)
            if result is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json({"ok": "error" not in result, **result})
        elif path == "/api/apply-replacements":
            result = self.store.apply_replacements(name)
            if result is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json({"ok": True, **result})
        elif path == "/api/export":
            result = self.store.export_project(name)
            if result is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json({"ok": True, **result})
        elif path == "/api/add-source":
            body = self._read_body() or {}
            result = self.store.add_source(body.get("dir") or self.source_dir,
                                           body.get("file") or "",
                                           body.get("mode") or "ask")
            self._send_json({"ok": not result.get("error"), **result})
        elif path == "/api/job":
            body = self._read_body() or {}
            do_t = bool(body.get("transcribe"))
            do_d = bool(body.get("diarize"))
            # 入っていないものは走らせない。押してから失敗するのを避ける。
            if do_t and not self.caps.get("transcribe_mlx"):
                self._send_json({"error": "no_transcriber"}, 400)
                return
            if do_d and not self.caps.get("diarize"):
                self._send_json({"error": "no_diarizer"}, 400)
                return
            result = self.jobs.start(body.get("name") or name, do_t, do_d,
                                     body.get("speakers"), body.get("model")) or {}
            self._send_json({"ok": not result.get("error"), **result})
        else:
            self._send_json({"error": "unknown"}, 404)


def main():
    parser = argparse.ArgumentParser(description="トランスクリプト話者アノテーター")
    parser.add_argument("--dir", default=os.path.join(BASE_DIR, "recordings"),
                        help="収録（*_timecoded.txt と音声）のディレクトリ")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--replacements", default=DEFAULT_REPLACEMENTS,
                        help="置換辞書のJSON（既定: リポジトリ直下の replacements.json）")
    parser.add_argument("--source-dir", default=os.path.expanduser("~"),
                        help="「ファイルを追加」で最初に開くディレクトリ")
    args = parser.parse_args()

    Handler.store = Store(args.dir, args.replacements)
    Handler.jobs = JobRunner(Handler.store)
    Handler.caps = capabilities()
    Handler.source_dir = os.path.abspath(os.path.expanduser(args.source_dir))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("アノテーターを起動しました:", url)
    print("対象ディレクトリ:", Handler.store.data_dir)
    print("置換辞書:", Handler.store.replacements_path,
          "(%d件)" % len(Handler.store.load_replacements()))
    # 文字起こしと話者分離は「あれば使う」。無くてもアノテーターは動く。
    ready = [k for k, v in Handler.caps.items() if v and k != "ffprobe"]
    print("この環境でできること:", "／".join(ready) if ready
          else "アノテーションのみ（文字起こし・話者分離のツールは未導入）")
    print("停止するには Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します。")
        server.shutdown()


if __name__ == "__main__":
    main()
