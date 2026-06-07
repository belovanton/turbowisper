"""Whisper API client - compatible with OpenAI API and faster-whisper-server."""

import json
import re

import httpx

from .config import Config


def _fix_spacing(text: str) -> str:
    """Ensure a space follows sentence-ending punctuation when missing."""
    # Add space after . ! ? , ; if followed directly by a letter (not digit — preserves 1.0, 3.14)
    # Negative lookbehind/ahead to skip URLs (://) and ellipsis (...)
    text = re.sub(r'(?<!\.)([.!?,;])(?![\s\d.!?,;:\)\]»"\'—/])([^\s])', r'\1 \2', text)
    # Colon: only add space if not followed by // (URL) or digits (time 10:30)
    text = re.sub(r':(?![/\d\s])([^\s])', r': \1', text)
    # Collapse any double spaces
    text = re.sub(r'  +', ' ', text)
    return text.strip()


class WhisperAPIError(Exception):
    """Error communicating with Whisper API."""

    pass


class WhisperClient:
    """Client for OpenAI-compatible Whisper API."""

    def __init__(self, config: Config):
        self.config = config

    async def transcribe(self, audio_data: bytes) -> str:
        """
        Send audio to Whisper API and return transcription.

        Args:
            audio_data: WAV audio data as bytes

        Returns:
            Transcribed text
        """
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        files = {
            "file": ("audio.wav", audio_data, "audio/wav"),
        }

        data = {
            "model": "whisper-1",  # Ignored by faster-whisper-server but required by OpenAI
            "language": self.config.language,
            "response_format": "json",
            "prompt": "Use proper punctuation: commas, periods, question marks.",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.config.api_url,
                    headers=headers,
                    files=files,
                    data=data,
                )

                if response.status_code != 200:
                    raise WhisperAPIError(f"API returned {response.status_code}: {response.text}")

                result = response.json()
                return result.get("text", "").strip()

        except httpx.TimeoutException:
            raise WhisperAPIError("Request timed out")
        except httpx.RequestError as e:
            raise WhisperAPIError(f"Request failed: {e}")
        except Exception as e:
            raise WhisperAPIError(f"Unexpected error: {e}")

    def transcribe_stream(
        self, audio_data: bytes, chunk_callback: callable
    ) -> str:
        """Transcribe with streaming — calls chunk_callback(text) for each segment.

        Falls back to regular transcription if server doesn't support streaming.

        Args:
            audio_data: WAV audio data as bytes
            chunk_callback: Called with each text chunk as it arrives

        Returns:
            Full transcribed text
        """
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        files = {"file": ("audio.wav", audio_data, "audio/wav")}
        data = {
            "model": "whisper-1",
            "language": self.config.language,
            "response_format": "json",
            "prompt": "Привет. Как дела? Всё хорошо, спасибо. Сегодня отличная погода! Давай обсудим детали.",
            "stream": "true",
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                with client.stream(
                    "POST",
                    self.config.api_url,
                    headers=headers,
                    files=files,
                    data=data,
                ) as response:
                    if response.status_code == 401:
                        raise WhisperAPIError("Unauthorized - check your API key in settings")
                    elif response.status_code == 403:
                        raise WhisperAPIError("Access denied - check your API key permissions")
                    elif response.status_code == 404:
                        raise WhisperAPIError("API endpoint not found - check your API URL")
                    elif response.status_code >= 500:
                        raise WhisperAPIError("Server error - try again later")
                    elif response.status_code != 200:
                        raise WhisperAPIError(f"API error ({response.status_code})")

                    content_type = response.headers.get("content-type", "")
                    full_text = ""

                    # Server-Sent Events stream
                    if "text/event-stream" in content_type:
                        for line in response.iter_lines():
                            line = line.strip()
                            if not line or line.startswith(":"):
                                continue
                            if line.startswith("data: "):
                                payload = line[6:]
                                if payload == "[DONE]":
                                    break
                                try:
                                    obj = json.loads(payload)
                                    segment_text = obj.get("text", "")
                                    if segment_text:
                                        full_text += segment_text
                                        chunk_callback(_fix_spacing(full_text))
                                except json.JSONDecodeError:
                                    pass
                        return _fix_spacing(full_text)

                    # NDJSON stream (faster-whisper-server with stream=true)
                    elif "application/x-ndjson" in content_type or "application/json" in content_type:
                        buffer = ""
                        for chunk in response.iter_bytes():
                            buffer += chunk.decode("utf-8", errors="replace")
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    obj = json.loads(line)
                                    obj_type = obj.get("type", "")
                                    if obj_type == "segment":
                                        seg_text = obj.get("text", "")
                                        if seg_text:
                                            full_text += seg_text
                                            chunk_callback(_fix_spacing(full_text))
                                    elif obj_type == "done":
                                        break
                                    elif obj_type not in ("info", ""):
                                        text = obj.get("text", "")
                                        if text:
                                            full_text = text
                                            chunk_callback(_fix_spacing(full_text))
                                except json.JSONDecodeError:
                                    pass
                        # Handle remaining buffer (plain JSON or last chunk)
                        if buffer.strip() and not full_text:
                            try:
                                obj = json.loads(buffer.strip())
                                full_text = obj.get("text", "").strip()
                                if full_text:
                                    chunk_callback(_fix_spacing(full_text))
                            except json.JSONDecodeError:
                                pass
                        return _fix_spacing(full_text)

                    else:
                        # Unknown content-type — read full body as plain JSON
                        response.read()
                        result = response.json()
                        text = _fix_spacing(result.get("text", ""))
                        if text:
                            chunk_callback(text)
                        return text

        except WhisperAPIError:
            raise
        except httpx.TimeoutException:
            raise WhisperAPIError("Request timed out - server may be busy")
        except httpx.ConnectError:
            raise WhisperAPIError("Could not connect - check internet/API URL")
        except httpx.RequestError as e:
            raise WhisperAPIError(f"Connection error: {e}")

    def transcribe_sync(self, audio_data: bytes) -> str:
        """Synchronous version of transcribe."""
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        files = {
            "file": ("audio.wav", audio_data, "audio/wav"),
        }

        data = {
            "model": "whisper-1",
            "language": self.config.language,
            "response_format": "json",
            "prompt": "Привет. Как дела? Всё хорошо, спасибо. Сегодня отличная погода! Давай обсудим детали.",
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self.config.api_url,
                    headers=headers,
                    files=files,
                    data=data,
                )

                if response.status_code == 401:
                    raise WhisperAPIError("Unauthorized - check your API key in settings")
                elif response.status_code == 403:
                    raise WhisperAPIError("Access denied - check your API key permissions")
                elif response.status_code == 404:
                    raise WhisperAPIError("API endpoint not found - check your API URL")
                elif response.status_code >= 500:
                    raise WhisperAPIError("Server error - try again later")
                elif response.status_code != 200:
                    raise WhisperAPIError(f"API error ({response.status_code})")

                result = response.json()
                return _fix_spacing(result.get("text", ""))

        except httpx.TimeoutException:
            raise WhisperAPIError("Request timed out - server may be busy")
        except httpx.ConnectError:
            raise WhisperAPIError("Could not connect - check internet/API URL")
        except httpx.RequestError as e:
            raise WhisperAPIError(f"Connection error: {e}")
