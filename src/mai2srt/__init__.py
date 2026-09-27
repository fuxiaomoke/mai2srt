"""mai2srt — playground.microsoft.ai MAI-Transcribe-2 to subtitles.

Pipeline: audio -> (probe/compress/chunk) -> browser-context transcription
-> normalized word-timestamp JSON -> [M2+] LLM segmentation -> SRT.

All playground protocol knowledge lives in `transcribe.client`; the shapes
were validated by the M0 spike (see _research/RESEARCH.md).
"""
__version__ = "0.1.2"
