"""Transcript acquisition, parsing and segmentation.

Everything here is pure except `extract_with_command`, which runs a user-supplied
command template with no shell involved.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

TIMESTAMP = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)
TEXT_CODECS = {"srt", "ass", "ssa", "mov_text", "subrip"}


def parse_srt(text: str) -> list[dict]:
    """Parse SRT into [{start_ms, end_ms, text}], sorted, blank cues dropped."""
    lines = [line.rstrip("\ufeff").rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    cues: list[dict] = []
    index = 0
    while index < len(lines):
        match = TIMESTAMP.search(lines[index])
        if not match:
            index += 1
            continue
        start = _ms(*match.group(1, 2, 3, 4))
        end = _ms(*match.group(5, 6, 7, 8))
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(_strip_tags(lines[index]))
            index += 1
        text_body = " ".join(part for part in body if part).strip()
        if text_body:
            cues.append({"start_ms": start, "end_ms": max(end, start), "text": text_body})
    cues.sort(key=lambda cue: cue["start_ms"])
    return cues


def _ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def _strip_tags(line: str) -> str:
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"\{\\[^}]*\}", "", line)
    return line.replace("♪", "").strip()


def dialogue_segments(
    cues: list[dict],
    *,
    target_ms: int = 180_000,
    merge_gap_ms: int = 2_500,
    hard_break_ms: int = 45_000,
) -> list[dict]:
    """Group cues into blocks of roughly `target_ms`, split at long silences.

    Used when a title has no usable chapter markers. Boundaries come from the
    dialogue itself, so nothing is invented.
    """
    if not cues:
        return []
    segments: list[dict] = []
    current: list[dict] = []
    for cue in cues:
        if not current:
            current = [cue]
            continue
        gap = cue["start_ms"] - current[-1]["end_ms"]
        duration = cue["end_ms"] - current[0]["start_ms"]
        if gap > hard_break_ms or (duration >= target_ms and gap > merge_gap_ms):
            segments.append(_segment(current))
            current = [cue]
        else:
            current.append(cue)
    if current:
        segments.append(_segment(current))
    return segments


def _segment(cues: list[dict]) -> dict:
    return {
        "start_ms": cues[0]["start_ms"],
        "end_ms": cues[-1]["end_ms"],
        "lines": cues,
    }


def segments_from_chapters(chapters: list[dict], cues: list[dict]) -> list[dict]:
    """Slice the transcript by chapter markers; drops chapters with no dialogue."""
    segments: list[dict] = []
    for chapter in chapters:
        start = int(chapter.get("startTimeOffset") or 0)
        end = int(chapter.get("endTimeOffset") or 0)
        if end <= start:
            continue
        lines = [c for c in cues if start <= c["start_ms"] < end]
        if lines:
            segments.append({"start_ms": start, "end_ms": end, "lines": lines})
    return segments


def usable_chapters(chapters: list[dict], runtime_ms: int) -> bool:
    """Chapters are only worth using when there are several of sane length."""
    if len(chapters) < 4:
        return False
    spans = [
        int(c.get("endTimeOffset") or 0) - int(c.get("startTimeOffset") or 0)
        for c in chapters
    ]
    sane = [s for s in spans if 30_000 <= s <= max(runtime_ms, 60_000)]
    return len(sane) >= max(4, len(spans) // 2)


def choose_segments(metadata: dict, cues: list[dict]) -> tuple[list[dict], str]:
    """Return (segments, method) using the documented chain."""
    chapters = metadata.get("Chapter") or []
    runtime = int(metadata.get("duration") or 0)
    if usable_chapters(chapters, runtime):
        segments = segments_from_chapters(chapters, cues)
        if segments:
            return segments, "chapters"
    if cues:
        return dialogue_segments(cues), "dialogue_gaps"
    return [], "none"


def render_segment(segment: dict, limit_ms: int | None = None) -> str:
    """Transcript lines for a prompt, never past `limit_ms` (the segment end)."""
    cutoff = segment["end_ms"] if limit_ms is None else limit_ms
    out = []
    for line in segment["lines"]:
        if line["start_ms"] > cutoff:
            break
        out.append(f"[{line['start_ms'] // 1000}] {line['text']}")
    return "\n".join(out)


_REMOTE_PROGRAMS = {"ssh", "mosh", "scp", "rsync"}


def _quote_for_remote(value: str) -> str:
    """Quote a value so it survives BOTH the local split and the remote shell.

    ssh joins its argv and the remote shell re-parses the line, so a value that
    is only quoted for the local parse (shlex.quote) mangles on an apostrophe.
    Wrapping in double quotes inside a single-quoted remote command survives the
    local split untouched and parses correctly remotely; only the characters
    special inside double quotes need escaping.
    """
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("$", "\\$").replace("`", "\\`"))
    return f'"{escaped}"'


def render_argv(template: str, *, media_path: str, stream_id: str, output: Path) -> list[str]:
    """Tokenize the template first, then substitute into each token.

    Substituting before splitting was the bug: a value containing a space or an
    apostrophe either split into several arguments or broke the quoting of the
    token it was inserted into. Splitting first means the template's structure is
    fixed, and each placeholder becomes exactly the argument it stands for.

    For a remote program (ssh, mosh, scp, rsync) the value is additionally
    quoted for the remote shell, because ssh joins its argv and the far side
    parses the result a second time.
    """
    if not template.strip():
        return []
    tokens = shlex.split(template)
    if not tokens:
        return []
    program = tokens[0].rsplit("/", 1)[-1]
    remote = program in _REMOTE_PROGRAMS
    values = {
        "{media_path}": media_path,
        "{stream_id}": str(stream_id),
        "{output}": str(output),
    }
    rendered: list[str] = []
    for token in tokens:
        for placeholder, value in values.items():
            if placeholder in token:
                token = token.replace(placeholder, _quote_for_remote(value) if remote else value)
        rendered.append(token)
    return rendered


def extract_with_command(template: str, *, media_path: str, stream_id: str,
                         output: Path, timeout: int = 900) -> bool:
    """Run a user-supplied command template to produce an SRT.

    `{media_path}`, `{stream_id}` and `{output}` each become exactly one
    argument: the template is tokenized first and the value is substituted into
    the token, so spaces, apostrophes and shell metacharacters in a path cannot
    split or break it. Write the placeholders bare — `-i {media_path}`, never
    `-i "{media_path}"`.

    For ssh, quote the REMOTE command so it forms a single token:

        ssh host "ffmpeg -v error -i {media_path} -map 0:s:0 -f srt -"

    ssh joins its argv with spaces and the far side parses the line again, so
    remote placeholders are quoted a second time for that shell.
    """
    if not template.strip():
        return False
    argv = render_argv(template, media_path=media_path, stream_id=stream_id, output=output)
    if not argv:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(argv, check=True, timeout=timeout, capture_output=True)
    except (subprocess.SubprocessError, OSError):
        return False
    if output.exists() and output.stat().st_size > 0:
        return True
    # A command may stream the SRT to stdout instead of writing {output} itself,
    # which is how an ssh one-liner works: ssh host ffmpeg ... -f srt -
    if proc.stdout:
        output.write_bytes(proc.stdout)
    return output.exists() and output.stat().st_size > 0
