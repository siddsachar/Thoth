from pathlib import Path


def test_youtube_transcript_runtime_dependency_is_packaged():
    requirements = Path("requirements.txt").read_text(encoding="utf-8").splitlines()
    normalized = {
        line.strip().split(";", 1)[0].split("#", 1)[0].strip().lower()
        for line in requirements
    }

    assert "youtube-search" in normalized
    assert "youtube-transcript-api" in normalized


def test_youtube_tool_uses_transcript_loader():
    source = Path("tools/youtube_tool.py").read_text(encoding="utf-8")

    assert "YoutubeLoader" in source
    assert "youtube_transcript" in source
