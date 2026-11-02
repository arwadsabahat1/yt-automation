"""Word-timed captions. Whisper supplies when each word is spoken; the words shown come from the script,
so names like "Opus Clip" are always spelled right even when Whisper mishears them.
Outputs an ASS file (burned into the video: bold words, current word highlighted) and an SRT file
(uploaded to YouTube as closed captions, which also helps search)."""
import re
from difflib import SequenceMatcher
from pathlib import Path

from core.config import DATA_DIR
from core.log import get_logger

log = get_logger("captions")

WHISPER_DIR = DATA_DIR / "models" / "whisper"
Word = tuple[str, float, float]  # (text as written in the script, start, end)


def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def heard_words(audio: Path, model: str) -> list[Word]:
    from faster_whisper import WhisperModel  # heavy import; only when rendering
    whisper = WhisperModel(model, device="cpu", compute_type="int8", download_root=str(WHISPER_DIR))
    segments, _ = whisper.transcribe(str(audio), language="en", word_timestamps=True, vad_filter=False)
    return [(w.word.strip(), w.start, w.end) for seg in segments for w in seg.words]


def align(script_words: list[str], heard: list[Word], start: float, end: float) -> list[Word]:
    """Give every script word a time. Matched words take Whisper's timing; the rest are spread
    proportionally (by length) between the nearest known times, so nothing drifts across lines."""
    times: list[tuple[float, float] | None] = [None] * len(script_words)
    matcher = SequenceMatcher(a=[norm(w) for w in script_words], b=[norm(w[0]) for w in heard], autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            _, s, e = heard[block.b + k]
            times[block.a + k] = (max(start, s), min(end, e))

    result: list[Word] = []
    i = 0
    while i < len(script_words):
        if times[i]:
            result.append((script_words[i], *times[i]))
            i += 1
            continue
        j = i
        while j < len(script_words) and not times[j]:
            j += 1
        gap_start = result[-1][2] if result else start
        gap_end = times[j][0] if j < len(script_words) else end
        gap_end = max(gap_end, gap_start)
        weights = [max(1, len(norm(w))) for w in script_words[i:j]]
        cursor = gap_start
        for word, weight in zip(script_words[i:j], weights):
            length = (gap_end - gap_start) * weight / sum(weights)
            result.append((word, cursor, cursor + length))
            cursor += length
        i = j
    return result


def timed_lines(timings: dict, heard: list[Word]) -> list[list[Word]]:
    lines = []
    for line in timings["lines"]:
        inside = [w for w in heard if w[1] >= line["start"] - 0.15 and w[2] <= line["end"] + 0.15]
        lines.append(align(line["text"].split(), inside, line["start"], line["end"]))
    return lines


def chunks(words: list[Word], max_words: int, max_chars: int) -> list[list[Word]]:
    """Short caption groups, breaking after punctuation so phrases stay together."""
    groups, current = [], []
    for word in words:
        if current and (len(current) >= max_words or len(" ".join(w[0] for w in current + [word])) > max_chars):
            groups.append(current)
            current = []
        current.append(word)
        if word[0][-1:] in ".,?!:;":
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").upper()


def bgr(hex_color: str) -> str:
    """'#FFD400' -> ASS '&H0000D4FF' (ASS colours are alpha-blue-green-red)."""
    h = hex_color.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_document(lines: list[list[Word]], chapters: list[tuple[float, str]], style: dict,
                 width: int, height: int) -> str:
    accent, white = bgr(style["accent"]), "&H00FFFFFF"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{style['font']},{style['size']},{white},{white},&H00000000,&H90000000,0,0,0,0,100,100,1,0,1,{style['outline']},3,{style['alignment']},80,80,{style['margin_v']},1
Style: Chapter,{style['font']},{round(style['size'] * 0.55)},&H00000000,&H00000000,{accent},&H00000000,0,0,0,0,100,100,2,0,3,14,0,7,60,60,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    all_groups = [g for line in lines for g in chunks(line, style["max_words"], style["max_chars"])]
    for n, group in enumerate(all_groups):
        next_start = all_groups[n + 1][0][1] if n + 1 < len(all_groups) else group[-1][2] + 0.5
        group_end = min(group[-1][2] + 0.3, next_start)
        for k, (_, s, e) in enumerate(group):
            until = group[k + 1][1] if k + 1 < len(group) else group_end
            if until <= s:
                continue
            words = [f"{{\\c{accent}}}{ass_text(w)}{{\\c{white}}}" if i == k else ass_text(w)
                     for i, (w, _, _) in enumerate(group)]
            pop = r"{\fscx112\fscy112\t(0,90,\fscx100\fscy100)}" if k == 0 else ""
            events.append(f"Dialogue: 1,{ass_time(s)},{ass_time(until)},Caption,,0,0,0,,{pop}{' '.join(words)}")
    for start, name in chapters:
        events.append(f"Dialogue: 2,{ass_time(start)},{ass_time(start + 3.2)},Chapter,,0,0,0,,"
                      f"{{\\fad(250,350)}} {ass_text(name)} ")
    return header + "\n".join(events) + "\n"


def srt_document(lines: list[list[Word]]) -> str:
    """One cue per caption group of up to ~8 words: readable closed captions for YouTube."""
    def t(x: float) -> str:
        ms = round(x * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    cues = [g for line in lines for g in chunks(line, 8, 42)]
    return "\n".join(f"{n}\n{t(g[0][1])} --> {t(g[-1][2])}\n{' '.join(w[0] for w in g)}\n"
                     for n, g in enumerate(cues, 1))
