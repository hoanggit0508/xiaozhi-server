import asyncio
import collections
import ctypes
import datetime
import io
import json
import logging
import math
import os
import re
import socket
import struct
import time
import urllib.request
import urllib.parse
import uuid
import wave
import array
from typing import Dict, Any, List, Optional

from aiohttp import web
import av
from ddgs import DDGS
import edge_tts
from gtts import gTTS
import miniaudio
from openai import AsyncOpenAI
import speech_recognition as sr
import yt_dlp
import ctypes.util

# ==================== 1. QUẢN LÝ NHẬT KÝ (LOGS) ====================
LOG_BUFFER = collections.deque(maxlen=250)

class WebLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
        except Exception:
            pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("XiaozhiServer")
logger.addHandler(WebLogHandler())

# ==================== TRÌNH ĐIỀU KHIỂN BỘ MÃ HÓA / GIẢI MÃ OPUS (TỰ ĐỘNG TƯƠNG THÍCH MỌI NỀN TẢNG) ====================
class OpusModule:
    OPUS_APPLICATION_VOIP = 2048
    OPUS_APPLICATION_AUDIO = 2049
    opus_int16 = ctypes.c_int16

    def __init__(self):
        self._lib = self._load_lib()
        if not self._lib:
            raise RuntimeError("Không tìm thấy thư viện chia sẻ libopus trên hệ điều hành này!")

        self._lib.opus_encoder_create.restype = ctypes.c_void_p
        self._lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]

        self._lib.opus_encode.restype = ctypes.c_int
        self._lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.opus_int16), ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]

        self._lib.opus_encoder_destroy.restype = None
        self._lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

        self._lib.opus_decoder_create.restype = ctypes.c_void_p
        self._lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]

        self._lib.opus_decode.restype = ctypes.c_int
        self._lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32, ctypes.POINTER(self.opus_int16), ctypes.c_int, ctypes.c_int]

        self._lib.opus_decoder_destroy.restype = None
        self._lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]

        self.opus_encoder_create = self._lib.opus_encoder_create
        self.opus_encode = self._lib.opus_encode
        self.opus_encoder_destroy = self._lib.opus_encoder_destroy
        self.opus_decoder_create = self._lib.opus_decoder_create
        self.opus_decode = self._lib.opus_decode
        self.opus_decoder_destroy = self._lib.opus_decoder_destroy

    def _load_lib(self):
        # 1. Thử pyogg nếu thư viện pyogg đã nạp sẵn được dll
        try:
            from pyogg import opus as pyogg_opus
            if hasattr(pyogg_opus, "opus_encoder_create") and hasattr(pyogg_opus, "opus_encode"):
                return pyogg_opus
        except Exception:
            pass

        # 2. Quét tất cả các đường dẫn thư viện Opus trên Linux (Debian, Ubuntu, Alpine), macOS và Windows
        candidates = [
            "opus",
            "libopus.so.0",
            "libopus.so",
            "/usr/lib/x86_64-linux-gnu/libopus.so.0",
            "/usr/lib/x86_64-linux-gnu/libopus.so",
            "/usr/lib/aarch64-linux-gnu/libopus.so.0",
            "/usr/lib/libopus.so.0",
            "/usr/lib/libopus.so",
            "/usr/lib64/libopus.so.0",
            "/usr/local/lib/libopus.so",
            "/usr/local/lib/libopus.so.0",
            "libopus-0.dll",
            "opus.dll"
        ]

        # Kiểm tra thêm trong thư mục cài đặt pyogg nếu có
        try:
            import pyogg
            p_dir = os.path.dirname(pyogg.__file__)
            for f in os.listdir(p_dir):
                if "opus" in f.lower() and f.endswith((".dll", ".so", ".dylib")):
                    candidates.insert(0, os.path.join(p_dir, f))
        except Exception:
            pass

        for cand in candidates:
            try:
                p = ctypes.util.find_library(cand) or cand
                lib = ctypes.CDLL(p)
                if hasattr(lib, "opus_encoder_create"):
                    logger.info(f"✅ Đã kết nối thư viện libopus thành công từ: {cand}")
                    return lib
            except Exception:
                continue
        return None

try:
    opus = OpusModule()
except Exception as e:
    logger.error(f"❌ Cảnh báo khởi tạo Opus: {e}")
    try:
        from pyogg import opus
    except Exception:
        opus = None

# ==================== 2. ĐỌC FILE CẤU HÌNH & KHỞI TẠO ====================
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

openai_client = AsyncOpenAI(
    base_url=os.environ.get("LLM_API_BASE") or CONFIG["llm"].get("api_base", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or CONFIG["llm"].get("api_key", "sk-placeholder")
)

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.102.49"

LOCAL_IP = get_local_ip()
ACTIVE_SESSION = None
LAST_ROBOT_IP = None
MCP_COUNTER = 1000
recognizer = sr.Recognizer()
VOICE_CACHE: Dict[str, List[bytes]] = {}
PREAMP_GAIN = float(CONFIG.get("tts", {}).get("gain_boost", 2.4))

# ==================== 3. KHUẾCH ĐẠI ÂM LƯỢNG & XỬ LÝ PCM ====================
def boost_pcm16_volume(pcm_data: bytes, gain: float = 2.4) -> bytes:
    """
    Khuếch đại biên độ âm thanh PCM 16-bit kỹ thuật số (Preamp Booster)
    Giúp loa trên xe robot phát to, rõ ràng và trong trẻo, không bị vỡ tiếng.
    """
    if not pcm_data or gain <= 1.0:
        return pcm_data
    try:
        samples = array.array('h', pcm_data)
        for i in range(len(samples)):
            val = int(samples[i] * gain)
            if val > 32767:
                val = 32767
            elif val < -32768:
                val = -32768
            samples[i] = val
        return samples.tobytes()
    except Exception:
        return pcm_data

def normalize_pcm16(pcm_data: bytes, target_peak: int = 24000) -> bytes:
    """Loại bỏ độ lệch DC và tự động khuếch đại tín hiệu micro robot ESP32 giúp nghe rõ ngay cả khi nói nhỏ hoặc đứng xa"""
    if not pcm_data:
        return b""
    try:
        samples = array.array('h', pcm_data)
        n = len(samples)
        if not n:
            return pcm_data
        mean = sum(samples) / n
        centered = [s - mean for s in samples]
        max_val = max(abs(x) for x in centered)
        if max_val < 30:  # Chỉ bỏ qua khi là độ tĩnh hoàn toàn (silence)
            return pcm_data
        gain = min(target_peak / max(max_val, 40.0), 16.0)
        if gain > 1.15 or abs(mean) > 40:
            samples = array.array('h', [int(min(max(x * gain, -32767), 32767)) for x in centered])
        return samples.tobytes()
    except Exception:
        return pcm_data

# ==================== 4. ĐIỀU KHIỂN PHẦN CỨNG XE QUA HTTP TRỰC TIẾP ====================
def _http_post_sync(url: str, payload_dict: dict, timeout: float = 3.5):
    for attempt in range(2):
        try:
            data = json.dumps(payload_dict).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status
        except Exception as e:
            if attempt == 0:
                time.sleep(0.15)
                continue
            logger.warning(f"Lỗi gửi lệnh phần cứng tới {url}: {e}")
            return None

async def send_robot_hardware_action(action: str, duration_ms: int = 0, speed: int = 85):
    """
    Điều khiển trực tiếp mạch cầu H và động cơ bánh xe:
    1. Ưu tiên gửi trực tiếp qua luồng kết nối WebSocket bảo mật của Robot (hỗ trợ cả Render Cloud 24/7).
    2. Nếu đang chạy mạng LAN nội bộ thì gửi bổ trợ qua REST API cổng 80 của xe.
    """
    global ACTIVE_SESSION, LAST_ROBOT_IP

    # 1. Ưu tiên gửi lệnh trực tiếp qua WebSocket tới Robot
    if ACTIVE_SESSION is not None:
        try:
            mcp_tool_map = {
                "forward": ("self.motor.move_forward", {"speed_percent": speed, "duration_ms": 10000}),
                "backward": ("self.motor.move_backward", {"speed_percent": speed, "duration_ms": 10000}),
                "left": ("self.motor.turn_left", {}),
                "right": ("self.motor.turn_right", {}),
                "spin": ("self.motor.spin_around", {}),
                "spin_around": ("self.motor.spin_around", {}),
                "stop": ("self.motor.stop", {}),
                "dance": ("self.motor.dance", {}),
                "victory": ("self.motor.dance", {}),
                "victory_dance": ("self.motor.dance", {}),
                "sway": ("self.motor.dance", {}),
                "sway_dance": ("self.motor.dance", {}),
                "patrol": ("self.motor.dance", {}),
                "happy": ("self.motor.happy", {}),
                "sad": ("self.motor.sad", {}),
                "wake_up": ("self.motor.wake_up", {}),
                "turn45_left": ("self.motor.turn_left", {}),
                "turn45_right": ("self.motor.turn_right", {}),
                "turn90_left": ("self.motor.turn_left", {}),
                "turn90_right": ("self.motor.turn_right", {}),
                "turn180": ("self.motor.spin_around", {}),
            }
            if action in mcp_tool_map:
                tool, args = mcp_tool_map[action]
                await ACTIVE_SESSION.execute_mcp_tool(tool, args)
                logger.info(f"🏎️ [MCP WebSocket 0ms] Đã gửi lệnh '{action}' trực tiếp tới xe qua WebSocket!")
                return
        except Exception as e:
            logger.warning(f"Lỗi gửi lệnh qua WebSocket: {e}")

    # 2. Nếu không có kết nối WebSocket và không có IP xe thật
    if not LAST_ROBOT_IP:
        logger.warning("⚠️ Xe Robot chưa kết nối vào Server. Vui lòng bật nguồn xe robot và kết nối Wifi!")
        return

    url_control = f"http://{LAST_ROBOT_IP}/api/control"
    url_action = f"http://{LAST_ROBOT_IP}/api/motor/action"

    special_actions = {
        "dance": "dance",
        "spin": "spin_around",
        "spin_around": "spin_around",
        "wake_up": "wake_up",
        "happy": "happy",
        "sad": "sad",
        "wiggle": "wiggle",
        "victory": "victory",
        "victory_dance": "victory_dance",
        "sway": "sway",
        "sway_dance": "sway_dance",
        "patrol": "patrol",
        "turn45_left": "turn45_left",
        "turn45_right": "turn45_right",
        "turn90_left": "turn90_left",
        "turn90_right": "turn90_right",
        "turn180": "turn180"
    }

    directions = {
        "forward": 4,
        "backward": 2,
        "left": 3,
        "right": 1,
        "stop": 0
    }

    loop = asyncio.get_running_loop()

    if action in special_actions:
        logger.info(f"🏎️ [HARDWARE 0ms] Kích hoạt hành động liên tục: {action}")
        await loop.run_in_executor(None, _http_post_sync, url_action, {"action": special_actions[action]})
    elif action in directions:
        dir_code = directions[action]
        logger.info(f"🏎️ [HARDWARE 0ms] Di chuyển hướng code={dir_code} ({action}) - tốc độ={speed}% (Chạy liên tục cho đến khi lệnh Dừng lại)")
        await loop.run_in_executor(None, _http_post_sync, url_control, {"direction": dir_code, "speed": speed})

# ==================== 5. GIẢI MÃ MICRO THỜI GIAN THỰC ====================
class OpusStreamDecoder:
    def __init__(self, sample_rate=16000, channels=1):
        self.sample_rate = sample_rate
        self.channels = channels
        err = ctypes.c_int()
        self.decoder = opus.opus_decoder_create(sample_rate, channels, ctypes.byref(err))
        self.pre_roll = collections.deque(maxlen=6)
        self.pcm_chunks: List[bytes] = []
        self.max_frame_samples = 5760
        self.speech_started = False
        self.speech_frame_count = 0
        self.vad_trigger_count = 0
        self.noise_floor = 110.0
        self.speech_start_time = 0.0
        self.last_voice_time = 0.0

    def add_packet(self, data: bytes) -> bool:
        """Giải mã gói Opus và chạy bộ phát hiện giọng nói thông minh (Energy VAD).
        Trả về True nếu vừa phát hiện bắt đầu giọng nói mới."""
        if not self.decoder or not data:
            return False
        just_started = False
        try:
            ubuf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            pbuf = ctypes.cast(ubuf, ctypes.POINTER(ctypes.c_ubyte))
            out_buf = (opus.opus_int16 * (self.max_frame_samples * self.channels))()
            samples = opus.opus_decode(
                self.decoder, 
                pbuf, 
                len(data), 
                out_buf, 
                self.max_frame_samples, 
                0
            )
            if samples > 0:
                raw_pcm = ctypes.string_at(out_buf, samples * 2 * self.channels)
                pcm_arr = array.array('h', raw_pcm)
                n = len(pcm_arr)
                if n > 0:
                    mean = sum(pcm_arr) / n
                    rms = math.sqrt(sum((s - mean) * (s - mean) for s in pcm_arr) / n)
                else:
                    rms = 0.0

                now = time.time()
                start_thresh = max(175.0, min(self.noise_floor * 2.3, 850.0))
                hold_thresh = max(130.0, min(self.noise_floor * 1.7, 600.0))

                if not self.speech_started:
                    self.pre_roll.append(raw_pcm)
                    if rms >= start_thresh:
                        self.vad_trigger_count += 1
                        if self.vad_trigger_count >= 2 or rms >= start_thresh * 1.8:
                            self.speech_started = True
                            self.speech_start_time = now
                            self.last_voice_time = now
                            self.pcm_chunks = list(self.pre_roll)
                            self.pre_roll.clear()
                            self.speech_frame_count = self.vad_trigger_count
                            just_started = True
                    else:
                        self.vad_trigger_count = 0
                        # Cập nhật nền nhiễu phòng theo thời gian thực
                        self.noise_floor = 0.92 * self.noise_floor + 0.08 * min(rms, 500.0)
                else:
                    self.pcm_chunks.append(raw_pcm)
                    if rms >= hold_thresh:
                        self.last_voice_time = now
                        self.speech_frame_count += 1
        except Exception as e:
            logger.warning(f"Lỗi giải mã audio frame: {e}")
        return just_started

    def reset(self):
        self.pre_roll.clear()
        self.pcm_chunks.clear()
        self.speech_started = False
        self.speech_frame_count = 0
        self.vad_trigger_count = 0
        self.speech_start_time = 0.0
        self.last_voice_time = 0.0

    def get_wav_data(self) -> bytes:
        raw_pcm = b"".join(self.pcm_chunks) if self.pcm_chunks else b"".join(self.pre_roll)
        if not raw_pcm:
            return b""
        raw_pcm = normalize_pcm16(raw_pcm)
        with io.BytesIO() as wav_io:
            with wave.open(wav_io, "wb") as wav_file:
                wav_file.setnchannels(self.channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(raw_pcm)
            return wav_io.getvalue()

    def __del__(self):
        if hasattr(self, 'decoder') and self.decoder:
            try:
                opus.opus_decoder_destroy(self.decoder)
            except Exception:
                pass

def clean_repetitive_text(text: str) -> str:
    text = text.strip()
    words = text.split()
    if not words:
        return ""
    for n in range(1, len(words) // 2 + 1):
        block = words[:n]
        k = len(words) // n
        if words == block * k and k >= 2:
            return " ".join(block)
    return text

def transcribe_audio_pcm(wav_bytes: bytes, preferred_lang: str = "vi-VN") -> str:
    """Chuyển đổi âm thanh thu từ Micro Robot sang Tiếng Việt hoặc Tiếng Anh (hỗ trợ luyện IELTS song ngữ)"""
    if not wav_bytes or len(wav_bytes) < 2500:
        return ""
    try:
        with io.BytesIO(wav_bytes) as audio_file:
            with sr.AudioFile(audio_file) as source:
                audio_data = recognizer.record(source)
                try:
                    text = recognizer.recognize_google(audio_data, language=preferred_lang)
                    if text:
                        return clean_repetitive_text(text)
                except sr.UnknownValueError:
                    pass
                except sr.RequestError as e:
                    logger.warning(f"Lỗi kết nối dịch vụ Google STT ({preferred_lang}): {e}")
                    return ""
                
                # Thử ngôn ngữ thay thế: nếu đang ở vi-VN thử en-US (phục vụ nói tiếng Anh/IELTS) và ngược lại
                alt_lang = "en-US" if preferred_lang == "vi-VN" else "vi-VN"
                try:
                    text = recognizer.recognize_google(audio_data, language=alt_lang)
                    return clean_repetitive_text(text)
                except sr.UnknownValueError:
                    return ""
                except sr.RequestError as e:
                    logger.warning(f"Lỗi kết nối dịch vụ Google STT ({alt_lang}): {e}")
                    return ""
    except Exception as e:
        logger.warning(f"Lỗi nhận dạng tiếng nói: {e}")
        return ""

# ==================== 6. TÌM KIẾM TRỰC TUYẾN TOÀN DIỆN (GOOGLE + WIKIPEDIA + AI) ====================
def get_current_datetime() -> str:
    now = datetime.datetime.now()
    days_vi = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
    day_str = days_vi[now.weekday()]
    return f"Dạ, hôm nay là {day_str}, ngày {now.day} tháng {now.month} năm {now.year}, hiện tại là {now.hour} giờ {now.minute} phút bạn nhé!"

def get_live_weather(query: str = "") -> str:
    loc = "Vietnam"
    q_lower = query.lower()
    loc_name = "Việt Nam"
    if "hà nội" in q_lower or "ha noi" in q_lower:
        loc = "Hanoi"
        loc_name = "Hà Nội"
    elif "đà nẵng" in q_lower or "da nang" in q_lower:
        loc = "Da_Nang"
        loc_name = "Đà Nẵng"
    elif "sài gòn" in q_lower or "hồ chí minh" in q_lower or "tphcm" in q_lower:
        loc = "Ho_Chi_Minh"
        loc_name = "Thành phố Hồ Chí Minh"
    elif "hải phòng" in q_lower or "hai phong" in q_lower:
        loc = "Hai_Phong"
        loc_name = "Hải Phòng"
    elif "cần thơ" in q_lower or "can tho" in q_lower:
        loc = "Can_Tho"
        loc_name = "Cần Thơ"
    
    try:
        url = f"https://wttr.in/{loc}?format=%C+%t"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = resp.read().decode("utf-8").strip()
            if data:
                translations = {
                    "Light rain shower": "Mưa rào nhẹ",
                    "Light rain": "Mưa nhỏ rải rác",
                    "Partly cloudy": "Trời có mây rải rác",
                    "Sunny": "Trời nắng đẹp",
                    "Clear": "Trời trong xanh quang đãng",
                    "Overcast": "Trời nhiều mây",
                    "Thunderstorm": "Có dông bão",
                    "Patchy rain possible": "Có thể có mưa vài nơi",
                    "Moderate rain": "Mưa vừa",
                    "Heavy rain": "Mưa to"
                }
                for en, vi in translations.items():
                    data = data.replace(en, vi)
                return f"Dạ, thời tiết tại {loc_name} hiện nay là {data} bạn nhé!"
    except Exception as e:
        logger.warning(f"Lỗi tra cứu thời tiết: {e}")

    return f"Dạ, hôm nay thời tiết tại {loc_name} rất đẹp và thoáng mát bạn nhé!"

def search_wikipedia_vi(query: str) -> List[str]:
    """Tra cứu bách khoa toàn thư Wikipedia tiếng Việt siêu nhanh (0.2s)"""
    try:
        clean_q = re.sub(r'^(ai là|là gì|ở đâu|người|tìm hiểu về)\s+', '', query, flags=re.IGNORECASE).strip()
        url = f"https://vi.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(clean_q or query)}&utf8=&format=json&srlimit=2"
        req = urllib.request.Request(url, headers={"User-Agent": "XiaozhiAI/2.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("query", {}).get("search", [])
            snippets = []
            for r in results:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                snippet = re.sub(r'<[^>]+>', '', snippet).strip()
                if snippet and len(snippet) > 20:
                    snippets.append(f"{title}: {snippet}")
            return snippets
    except Exception as e:
        logger.debug(f"Wiki lookup info: {e}")
        return []

def search_duckduckgo_online(query: str) -> List[str]:
    """Tìm kiếm dữ liệu toàn bộ Internet mới nhất qua DDGS"""
    try:
        ddgs = DDGS(timeout=4.0)
        results = list(ddgs.text(query, max_results=3))
        snippets = []
        for r in results:
            body = r.get("body", "").strip()
            if body and len(body) > 15:
                snippets.append(body)
        return snippets
    except Exception as e:
        logger.warning(f"DDGS search issue: {e}")
        return []

async def answer_user_query_online(query: str, history: List[dict]) -> str:
    """
    Tìm kiếm toàn bộ thông tin trực tuyến (Google / DDG / Wikipedia)
    và trò chuyện tâm sự đa lĩnh vực với người dùng một cách tự nhiên, sâu sắc, thông minh như người thật!
    """
    logger.info(f"🧠 [AI THINKING] Người dùng hỏi/tâm sự: '{query}'")
    loop = asyncio.get_running_loop()

    # Nhận diện xem câu nói là tìm kiếm dữ liệu/thời sự/kiến thức hay là trò chuyện/tâm sự/cảm xúc
    query_lower = query.lower().strip()
    is_emotional_or_casual = any(w in query_lower for w in [
        "buồn", "vui", "mệt", "tâm sự", "bạn nghĩ sao", "cảm thấy", "lời khuyên", 
        "chia sẻ", "yêu", "ghét", "hạnh phúc", "cô đơn", "chán", "áp lực", "stress",
        "tại sao mình", "giúp mình", "ý nghĩa", "cuộc sống", "tình bạn", "tình yêu",
        "kể chuyện", "tán ngẫu", "trò chuyện", "thơ", "hài hước", "tri kỷ"
    ])

    context_text = ""
    # Nếu câu hỏi mang tính tra cứu thông tin, sự kiện, lịch sử, văn hóa, khoa học:
    if not is_emotional_or_casual or any(k in query_lower for k in ["là ai", "là gì", "ở đâu", "khi nào", "bao nhiêu", "năm nào", "tin tức", "sự kiện", "lịch sử", "giá"]):
        logger.info(f"🔍 [ONLINE SEARCH] Đang tra cứu trực tuyến thời gian thực cho: '{query}'")
        try:
            ddg_task = loop.run_in_executor(None, search_duckduckgo_online, query)
            wiki_task = loop.run_in_executor(None, search_wikipedia_vi, query)
            ddg_res, wiki_res = await asyncio.gather(ddg_task, wiki_task)
            all_snippets = []
            if ddg_res: all_snippets.extend(ddg_res)
            if wiki_res: all_snippets.extend(wiki_res)
            context_text = "\n".join(all_snippets[:4])
        except Exception as e:
            logger.warning(f"Lỗi tìm kiếm trực tuyến: {e}")

    # Gọi mô hình LLM (Gemini 2.0 Flash / OpenAI) kết hợp ngữ cảnh và lịch sử cuộc trò chuyện
    try:
        system_instruction = CONFIG["llm"]["system_prompt"]
        if context_text:
            system_instruction += f"\n\n[DỮ LIỆU TRA CỨU TRỰC TUYẾN MỚI NHẤT TỪ GOOGLE/WIKIPEDIA]:\n{context_text}\nHãy vận dụng thông tin trực tuyến trên để giải đáp một cách thông minh, chính xác, tự nhiên và trọn vẹn nhất cho người dùng."

        messages = [{"role": "system", "content": system_instruction}]
        
        # Đưa các lượt trò chuyện gần nhất vào để ghi nhớ ngữ cảnh tâm sự
        for item in history[-8:]:
            if item.get("role") in ["user", "assistant"]:
                messages.append(item)
        
        messages.append({"role": "user", "content": query})

        response = await openai_client.chat.completions.create(
            model=CONFIG["llm"]["model"],
            messages=messages,
            temperature=CONFIG["llm"].get("temperature", 0.75),
            timeout=8.0
        )
        llm_reply = response.choices[0].message.content
        if llm_reply and len(llm_reply.strip()) > 3:
            clean_reply = llm_reply.strip()
            # Lưu lại vào lịch sử đàm thoại
            history.append({"role": "user", "content": query})
            history.append({"role": "assistant", "content": clean_reply})
            if len(history) > 20:
                history[:] = [history[0]] + history[-12:]
            return clean_reply
    except Exception as e:
        logger.warning(f"LLM API gặp sự cố ({e}), kích hoạt bộ giải đáp dự phòng thông minh.")

    # Bộ dự phòng nếu không kết nối được LLM:
    if context_text:
        combined = context_text.replace("\n", " ").strip()
        combined = re.sub(r'\s{2,}', ' ', combined)
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', combined) if len(s.strip()) > 20 and not s.startswith("http")]
        if sentences:
            summary = ". ".join(sentences[:2]).strip()
            if not summary.endswith("."): summary += "."
            return f"Dạ, theo thông tin em tra cứu trực tuyến được: {summary} Bạn cần em tìm hiểu thêm gì nữa không ạ?"

    return f"Dạ em hiểu ý bạn rồi ạ! Em luôn ở đây lắng nghe và đồng hành cùng bạn. Bạn muốn tâm sự hoặc hỏi thêm điều gì về {query} nữa không ạ?"

# ==================== 7. PHÁT NHẠC TRỰC TUYẾN ĐA NGUỒN (YOUTUBE + SOUNDCLOUD FALLBACK) ====================
_MUSIC_YDL_BASE_OPTS = {
    'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'socket_timeout': 10,
}

def _build_pyav_http_options(http_headers: Optional[dict]) -> dict:
    """Chuyển HTTP headers từ yt-dlp sang options cho FFmpeg/PyAV để tránh lỗi HTTP 403."""
    av_opts = {
        'reconnect': '1',
        'reconnect_streamed': '1',
        'reconnect_delay_max': '5',
    }
    if not http_headers:
        return av_opts
    extra_lines = []
    for key, val in http_headers.items():
        if not val:
            continue
        if key.lower() == 'user-agent':
            av_opts['user_agent'] = str(val)
        else:
            extra_lines.append(f"{key}: {val}\r\n")
    if extra_lines:
        av_opts['headers'] = "".join(extra_lines)
    return av_opts

def _open_verified_audio_stream(stream_url: str, http_headers: Optional[dict] = None):
    """Mở luồng âm thanh qua PyAV kèm headers và kiểm tra giải mã được frame đầu tiên."""
    container = av.open(stream_url, options=_build_pyav_http_options(http_headers), timeout=15.0)
    try:
        audio_stream = next((s for s in container.streams if s.type == 'audio'), None)
        if audio_stream is None:
            raise ValueError("Không tìm thấy luồng âm thanh trong container")
        decoder = container.decode(audio_stream)
        first_frame = next(decoder, None)
        if first_frame is None:
            raise ValueError("Luồng âm thanh rỗng (0 frame)")
        return container, audio_stream, first_frame, decoder
    except Exception:
        try:
            container.close()
        except Exception:
            pass
        raise

def _iter_audio_frames(first_frame, decoder):
    """Duyệt liên tiếp từ frame đã kiểm tra trước sang phần còn lại của luồng giải mã."""
    if first_frame is not None:
        yield first_frame
    yield from decoder

def _extract_yt_dlp_entries(target: str, youtube_clients: Optional[list] = None) -> list:
    """Chạy yt-dlp lấy danh sách metadata + link trực tiếp mà không tải file về đĩa."""
    opts = dict(_MUSIC_YDL_BASE_OPTS)
    if youtube_clients:
        opts['extractor_args'] = {'youtube': {'player_client': youtube_clients}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        return []
    entries = info.get('entries')
    if entries is not None:
        return [e for e in entries if e]
    return [info]

def _try_open_entry(entry: dict, source_label: str):
    """Thử mở và xác thực phát thực tế một entry từ yt-dlp."""
    stream_url = entry.get('url')
    if not stream_url:
        return None
    title = entry.get('title') or "Bài hát"
    try:
        container, audio_stream, first_frame, decoder = _open_verified_audio_stream(
            stream_url, entry.get('http_headers')
        )
        return title, container, audio_stream, first_frame, decoder, source_label
    except Exception as e:
        logger.warning(f"⚠️ [{source_label}] Không mở được luồng '{title}': {e}")
        return None

def _prioritize_soundcloud_entries(entries: list) -> list:
    """Ưu tiên bản nhạc đầy đủ (>= 60 giây) trước các đoạn preview ngắn 30 giây."""
    full_tracks = [e for e in entries if (e.get('duration') or 0) >= 60]
    short_tracks = [e for e in entries if 0 < (e.get('duration') or 0) < 60]
    unknown_tracks = [e for e in entries if not e.get('duration')]
    return full_tracks + short_tracks + unknown_tracks

def extract_and_open_music_stream(query: str):
    """
    Tìm kiếm bài hát và mở sẵn luồng PyAV đã kiểm tra hoạt động (tránh 403 trên máy chủ cloud).
    Thứ tự ưu tiên:
      1. YouTube (client android_sdkless -> mediaconnect -> ios_music kèm HTTP headers chuẩn)
      2. SoundCloud (scsearch5 — hoạt động ổn định trên IP datacenter khi YouTube chặn 403)
    Trả về: (title, container, audio_stream, first_frame, decoder, source_name) hoặc None.
    """
    search_q = (query or "").strip()
    if not search_q:
        return None

    # Trường hợp người dùng đưa link trực tiếp
    if search_q.startswith(("http://", "https://")):
        for clients, label in [(['android_sdkless', 'mediaconnect'], "Direct-YT"), (None, "Direct-URL")]:
            try:
                for entry in _extract_yt_dlp_entries(search_q, youtube_clients=clients):
                    opened = _try_open_entry(entry, label)
                    if opened:
                        return opened
            except Exception as e:
                logger.warning(f"⚠️ [{label}] Lỗi trích xuất URL '{search_q}': {e}")
        try:
            container, audio_stream, first_frame, decoder = _open_verified_audio_stream(search_q)
            return search_q, container, audio_stream, first_frame, decoder, "Raw-HTTP"
        except Exception as e:
            logger.warning(f"⚠️ [Raw-HTTP] Không mở được '{search_q}': {e}")
            return None

    # Bước 1: Thử tìm và mở trên YouTube với client ít bị chặn 403 nhất
    yt_entries = []
    try:
        yt_entries = _extract_yt_dlp_entries(f"ytsearch1:{search_q}", youtube_clients=['android_sdkless'])
    except Exception as e:
        logger.warning(f"⚠️ [YouTube Search] Không thể tìm '{search_q}' (chuyển sang SoundCloud): {e}")

    if yt_entries:
        first_entry = yt_entries[0]
        opened = _try_open_entry(first_entry, "YouTube (android_sdkless)")
        if opened:
            return opened

        # Nếu tìm được link video nhưng CDN từ chối 403, thử lại đúng URL đó bằng client dự phòng
        watch_url = first_entry.get('webpage_url')
        if watch_url:
            for alt_client in ('mediaconnect', 'ios_music'):
                try:
                    for alt_entry in _extract_yt_dlp_entries(watch_url, youtube_clients=[alt_client]):
                        opened = _try_open_entry(alt_entry, f"YouTube ({alt_client})")
                        if opened:
                            return opened
                except Exception as e:
                    logger.warning(f"⚠️ [YouTube ({alt_client})] Lỗi trích xuất '{watch_url}': {e}")

    # Bước 2: Dự phòng tự động sang SoundCloud (ổn định cao trên Render / IP cloud datacenter)
    try:
        sc_entries = _extract_yt_dlp_entries(f"scsearch5:{search_q}")
        for entry in _prioritize_soundcloud_entries(sc_entries):
            opened = _try_open_entry(entry, "SoundCloud")
            if opened:
                return opened
    except Exception as e:
        logger.warning(f"⚠️ [SoundCloud] Lỗi tìm kiếm '{search_q}': {e}")

    return None

def extract_youtube_audio_info(query: str):
    """Hàm tương thích ngược: lấy tiêu đề và URL âm thanh trực tiếp."""
    search_q = (query or "").strip()
    if not search_q:
        return query, None
    targets = [
        (search_q if search_q.startswith("http") else f"ytsearch1:{search_q}", ['android_sdkless', 'mediaconnect']),
        (None if search_q.startswith("http") else f"scsearch3:{search_q}", None),
    ]
    for target, clients in targets:
        if not target:
            continue
        try:
            entries = _extract_yt_dlp_entries(target, youtube_clients=clients)
            if target.startswith("scsearch"):
                entries = _prioritize_soundcloud_entries(entries)
            for entry in entries:
                if entry.get('url'):
                    return entry.get('title', query), entry.get('url')
        except Exception as e:
            logger.warning(f"⚠️ Trích xuất {target} thất bại: {e}")
    return query, None

# ==================== 8. TẠO ÂM THANH OPUS 24kHz & PREAMP BOOST ====================
def is_predominantly_english(text: str) -> bool:
    """Kiểm tra xem câu văn có phải hoàn toàn là tiếng Anh (không có dấu tiếng Việt) không"""
    if not text:
        return False
    vi_chars = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
                   "ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ")
    if any(c in vi_chars for c in text):
        return False
    words = re.findall(r'[a-zA-Z]+', text)
    return len(words) >= 3

async def generate_opus_frames(text: str) -> List[bytes]:
    """Chuyển đổi văn bản sang luồng gói OPUS 24kHz âm lượng lớn với bộ đệm giọng nói tức thì"""
    if not text:
        return []

    if text in VOICE_CACHE:
        return VOICE_CACHE[text]

    mp3_data = b""

    # Tự động chọn giọng đọc phù hợp: Nếu là câu thuần tiếng Anh thì dùng giọng bản xứ chuẩn IELTS (en-US-JennyNeural), nếu có tiếng Việt thì dùng vi-VN-HoaiMyNeural
    if is_predominantly_english(text):
        voice = "en-US-JennyNeural"
    else:
        voice = CONFIG["tts"].get("voice", "vi-VN-HoaiMyNeural")

    # 1. Thử Microsoft Edge-TTS với timeout 5.0s
    try:
        rate = CONFIG["tts"].get("rate", "+10%")
        comm = edge_tts.Communicate(text, voice, rate=rate)
        async def _fetch_edge():
            chunks = []
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)
        mp3_data = await asyncio.wait_for(_fetch_edge(), timeout=5.0)
    except Exception:
        pass

    # 2. Nếu Edge-TTS lỗi hoặc bận, dùng ngay Google TTS cực kỳ ổn định
    if not mp3_data:
        try:
            buf = io.BytesIO()
            loop = asyncio.get_running_loop()
            tts_lang = "en" if is_predominantly_english(text) else "vi"
            await loop.run_in_executor(None, lambda: gTTS(text=text, lang=tts_lang).write_to_fp(buf))
            mp3_data = buf.getvalue()
        except Exception as e:
            logger.error(f"Lỗi tạo giọng nói TTS: {e}")
            return []

    # 3. Giải mã MP3 thành PCM 24000Hz mono s16
    try:
        decoded = miniaudio.mp3_read_s16(mp3_data)
        pcm_bytes = decoded.samples.tobytes()

        if decoded.sample_rate != 24000 or decoded.nchannels != 1:
            pcm_bytes = bytes(miniaudio.convert_frames(
                miniaudio.SampleFormat.SIGNED16,
                decoded.nchannels,
                decoded.sample_rate,
                pcm_bytes,
                miniaudio.SampleFormat.SIGNED16,
                1,
                24000
            ))

        # 4. Khuếch đại âm lượng kỹ thuật số (NÓI TO HƠN)
        pcm_bytes = boost_pcm16_volume(pcm_bytes, gain=PREAMP_GAIN)

        # 5. Mã hóa sang các gói Opus 24000Hz (khung 60ms = 1440 mẫu = 2880 bytes)
        err = ctypes.c_int()
        enc = opus.opus_encoder_create(24000, 1, opus.OPUS_APPLICATION_VOIP, ctypes.byref(err))
        frame_samples = 1440
        frame_bytes = frame_samples * 2
        out_buf = (ctypes.c_ubyte * 1275)()
        opus_frames = []

        for i in range(0, len(pcm_bytes), frame_bytes):
            chunk = pcm_bytes[i:i+frame_bytes]
            if len(chunk) < frame_bytes:
                chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
            in_buf = (opus.opus_int16 * frame_samples).from_buffer_copy(chunk)
            p_in = ctypes.cast(in_buf, ctypes.POINTER(opus.opus_int16))
            p_out = ctypes.cast(out_buf, ctypes.POINTER(ctypes.c_ubyte))
            nbytes = opus.opus_encode(enc, p_in, frame_samples, p_out, 1275)
            if nbytes > 0:
                opus_frames.append(bytes(out_buf[:nbytes]))

        opus.opus_encoder_destroy(enc)
        return opus_frames

    except Exception as e:
        logger.error(f"Lỗi mã hóa Opus: {e}")
        return []

# ==================== 9. PHIÊN KẾT NỐI ROBOT (XIAOZHI SESSION) ====================
class XiaozhiSession:
    def __init__(self, ws):
        self.ws = ws
        self.session_id = f"session-{uuid.uuid4().hex[:8]}"
        self.protocol_version = 1
        self.audio_decoder = OpusStreamDecoder(sample_rate=16000, channels=1)
        self.conversation_history = [
            {"role": "system", "content": CONFIG["llm"]["system_prompt"]}
        ]
        self.preferred_lang = "vi-VN"
        self.is_listening = False
        self.last_audio_time = 0.0
        self.music_task: Optional[asyncio.Task] = None
        self.is_speaking = False
        self.is_processing = False
        self.echo_suppression_logged = False
        self.last_speaking_end_time = 0.0
        self._out_timestamp_ms = 0
        self.watcher_task = asyncio.create_task(self._silence_watcher())

    async def send_opus_packet(self, frame: bytes):
        """Gửi gói âm thanh Opus đúng theo version giao thức WebSocket của robot (v1, v2, v3)"""
        if self.ws.closed or not frame:
            return
        if self.protocol_version == 2:
            self._out_timestamp_ms = (self._out_timestamp_ms + 60) & 0xFFFFFFFF
            pkt = struct.pack(">HHIII", 2, 0, 0, self._out_timestamp_ms, len(frame)) + frame
        elif self.protocol_version == 3:
            pkt = struct.pack(">BBH", 0, 0, len(frame)) + frame
        else:
            pkt = frame
        await self.ws.send_bytes(pkt)

    async def _silence_watcher(self):
        """Tự động phát hiện khi người dùng nói xong (qua Energy VAD) để xử lý lệnh ngay lập tức"""
        try:
            while not self.ws.closed:
                await asyncio.sleep(0.15)
                if (
                    self.is_listening
                    and not self.is_processing
                    and not self.is_speaking
                    and self.audio_decoder.speech_started
                ):
                    now = time.time()
                    silence_after_voice = now - self.audio_decoder.last_voice_time
                    utt_duration = now - self.audio_decoder.speech_start_time
                    has_enough_speech = self.audio_decoder.speech_frame_count >= 3

                    if (silence_after_voice > 0.85 and has_enough_speech) or utt_duration > 14.0:
                        logger.info("⏱️ [VAD Silence] Đã phát hiện người dùng nói xong -> Đang nhận diện giọng nói...")
                        wav_data = self.audio_decoder.get_wav_data()
                        self.audio_decoder.reset()
                        if not wav_data:
                            continue

                        self.is_processing = True
                        try:
                            loop = asyncio.get_running_loop()
                            text = await loop.run_in_executor(
                                None, transcribe_audio_pcm, wav_data, self.preferred_lang
                            )
                            if text:
                                logger.info(f"🎤 [Micro Robot Nhận Diện]: '{text}'")
                                await self.handle_user_query(text)
                            else:
                                logger.info("🎙️ [VAD] Có âm thanh từ micro nhưng Google STT chưa nghe rõ chữ (hãy nói to rõ hơn gần mic robot).")
                        finally:
                            self.audio_decoder.reset()
                            self.is_processing = False
        except asyncio.CancelledError:
            pass

    async def send_json(self, data: Dict[str, Any]):
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(data))

    async def execute_mcp_tool(self, tool_name: str, args: Dict[str, Any]):
        """Gửi lệnh MCP điều khiển phần cứng tới robot với ID duy nhất"""
        global MCP_COUNTER
        MCP_COUNTER += 1
        mcp_id = MCP_COUNTER
        logger.info(f"🛠️ [MCP Tool Call #{mcp_id}] {tool_name} -> {args}")
        await self.send_json({
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": mcp_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": args
                }
            }
        })

    def stop_music(self):
        """Dừng luồng phát nhạc trực tuyến nếu đang chạy"""
        if self.music_task and not self.music_task.done():
            self.music_task.cancel()
            self.music_task = None
            logger.info("⏹️ Đã dừng luồng phát nhạc trực tuyến.")

    async def send_tts_speech(self, text: str, stop_music: bool = True):
        """Phát âm thanh Tiếng Việt qua Loa của Robot bằng chuẩn OPUS âm lượng to rõ ràng"""
        if not text or self.ws.closed:
            return

        if stop_music:
            self.stop_music()
        self.is_speaking = True
        logger.info(f"🗣️ Robot nói: {text}")
        opus_frames = await generate_opus_frames(text)
        if not opus_frames or self.ws.closed:
            self.is_speaking = False
            return

        # Báo bắt đầu phát âm thanh và hiển thị phụ đề lên màn hình OLED
        await self.send_json({
            "type": "tts",
            "state": "start",
            "session_id": self.session_id
        })
        await self.send_json({
            "type": "tts",
            "state": "sentence_start",
            "text": text,
            "session_id": self.session_id
        })
        await self.send_json({
            "type": "stt",
            "text": text
        })

        # Cho ESP32 120ms để chuyển trạng thái sang Speaking và mở DAC loa
        await asyncio.sleep(0.12)

        # 3. Truyền các gói Opus nhị phân cho loa ESP32 (mỗi khung 60ms, gửi đều 55ms)
        for frame in opus_frames:
            if self.ws.closed:
                break
            await self.send_opus_packet(frame)
            await asyncio.sleep(0.055)

        # 4. Báo kết thúc câu nói
        await self.send_json({
            "type": "tts",
            "state": "stop",
            "session_id": self.session_id
        })
        self.is_speaking = False
        self.last_speaking_end_time = time.time()

    async def stream_youtube_music(self, song_name: str):
        """
        Phát bất kỳ bài hát nào từ YouTube trực tiếp qua Loa của Robot
        Âm lượng to, chất lượng cao, độ trễ thấp và có thể dừng bất cứ lúc nào!
        """
        self.stop_music()

        intro_msg = f"Dạ, em đang mở bài {song_name} cho bạn đây ạ!"

        def _close_container_safe(cont):
            if cont is not None:
                try:
                    cont.close()
                except Exception:
                    pass

        def _close_future_container(fut):
            if not fut.cancelled():
                try:
                    res = fut.result()
                    if res and len(res) > 1:
                        _close_container_safe(res[1])
                except Exception:
                    pass

        async def _music_worker():
            container = None
            fetch_future = None
            tts_stop_sent = False
            try:
                logger.info(f"🎵 [MUSIC] Đang tìm kiếm và xác thực luồng âm thanh cho: '{song_name}'")
                loop = asyncio.get_running_loop()
                # Chạy song song: vừa nói câu giới thiệu vừa trích xuất & mở sẵn luồng phát để triệt tiêu độ trễ
                fetch_future = loop.run_in_executor(None, extract_and_open_music_stream, song_name)
                await self.send_tts_speech(intro_msg, stop_music=False)

                result = await fetch_future

                if not result:
                    await self.send_tts_speech(
                        "Dạ, em chưa tìm được nguồn phát ổn định cho bài hát này. Bạn thử nói tên bài khác nhé ạ!",
                        stop_music=False
                    )
                    return

                title, container, audio_stream, first_frame, decoder, source_name = result
                logger.info(f"🎶 [MUSIC PLAYING | {source_name}] Bắt đầu phát bài: '{title}'")

                # Báo cho ESP32 biết chuẩn bị phát nhạc
                await self.execute_mcp_tool("self.audio_speaker.set_volume", {"volume": 100})
                await self.send_json({
                    "type": "tts",
                    "state": "start",
                    "session_id": self.session_id
                })
                await self.send_json({
                    "type": "tts",
                    "state": "sentence_start",
                    "text": f"Đang phát: {title}",
                    "session_id": self.session_id
                })
                await asyncio.sleep(0.12)

                # Khởi tạo bộ resample & nén sang Opus 24kHz từ luồng PyAV đã mở sẵn
                resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)

                err = ctypes.c_int()
                enc = opus.opus_encoder_create(24000, 1, opus.OPUS_APPLICATION_AUDIO, ctypes.byref(err))
                frame_samples = 1440
                frame_bytes = frame_samples * 2
                out_buf = (ctypes.c_ubyte * 1275)()

                pcm_buffer = bytearray()

                try:
                    for frame in _iter_audio_frames(first_frame, decoder):
                        if self.ws.closed:
                            break

                        resampled_frames = resampler.resample(frame)
                        for r_frame in resampled_frames:
                            # Khuếch đại âm lượng nhạc (1.7x) để nghe to rõ qua loa
                            boosted_pcm = boost_pcm16_volume(bytes(r_frame.planes[0]), gain=1.7)
                            pcm_buffer.extend(boosted_pcm)

                            while len(pcm_buffer) >= frame_bytes:
                                chunk = bytes(pcm_buffer[:frame_bytes])
                                del pcm_buffer[:frame_bytes]

                                in_buf = (opus.opus_int16 * frame_samples).from_buffer_copy(chunk)
                                p_in = ctypes.cast(in_buf, ctypes.POINTER(opus.opus_int16))
                                p_out = ctypes.cast(out_buf, ctypes.POINTER(ctypes.c_ubyte))
                                nbytes = opus.opus_encode(enc, p_in, frame_samples, p_out, 1275)
                                if nbytes > 0:
                                    await self.send_opus_packet(bytes(out_buf[:nbytes]))
                                    await asyncio.sleep(0.057)

                    logger.info(f"🏁 Đã kết thúc phát bài hát: {title}")
                finally:
                    if enc:
                        opus.opus_encoder_destroy(enc)
                    _close_container_safe(container)
                    container = None
                    if not tts_stop_sent:
                        tts_stop_sent = True
                        await self.send_json({
                            "type": "tts",
                            "state": "stop",
                            "session_id": self.session_id
                        })

            except asyncio.CancelledError:
                if fetch_future and not fetch_future.done():
                    fetch_future.add_done_callback(_close_future_container)
                _close_container_safe(container)
                self.is_speaking = False
                if not tts_stop_sent:
                    tts_stop_sent = True
                    try:
                        await self.send_json({
                            "type": "tts",
                            "state": "stop",
                            "session_id": self.session_id
                        })
                    except Exception:
                        pass
                logger.info(f"⏹️ Luồng phát bài '{song_name}' đã được dừng theo yêu cầu.")
            except Exception as e:
                _close_container_safe(container)
                logger.error(f"Lỗi trong quá trình phát nhạc: {e}")
                await self.send_tts_speech(
                    "Dạ, luồng phát nhạc gặp gián đoạn nhẹ. Bạn vui lòng chọn lại bài nhé ạ!",
                    stop_music=False
                )

        self.music_task = asyncio.create_task(_music_worker())

    async def execute_fast_command(self, text: str) -> bool:
        text_lower = text.lower().strip()
        for pattern, cmd in CONFIG.get("fast_commands", {}).items():
            if re.search(pattern, text_lower):
                tool = cmd["tool"]
                reply = cmd.get("reply", "Dạ em thực hiện ngay đây ạ!")
                logger.info(f"⚡ [FAST-PATH 0ms] Kích hoạt lệnh tức thì: '{text}' -> Tool: {tool}")

                # 1. Điều khiển phần cứng tức thì KHÔNG AUTO DỪNG (duy trì liên tục cho đến khi lệnh Dừng lại)
                movement_map = {
                    "self.motor.move_forward": ("forward", 85),
                    "self.motor.move_backward": ("backward", 85),
                    "self.motor.turn_left": ("left", 80),
                    "self.motor.turn_right": ("right", 80),
                    "self.motor.spin_around": ("spin", 85),
                    "self.motor.stop": ("stop", 0),
                    "self.motor.dance": ("dance", 0),
                    "self.motor.happy": ("happy", 0),
                    "self.motor.sad": ("sad", 0),
                    "self.motor.wake_up": ("wake_up", 0),
                    "self.motor.victory_dance": ("victory_dance", 0),
                    "self.motor.sway_dance": ("sway_dance", 0),
                    "self.motor.patrol": ("patrol", 0),
                }

                args = cmd.get("args", {})
                if tool in movement_map:
                    act, spd = movement_map[tool]
                    mcp_args = dict(args)
                    if tool in ["self.motor.move_forward", "self.motor.move_backward"]:
                        if "speed_percent" not in mcp_args:
                            mcp_args["speed_percent"] = spd
                        if "duration_ms" not in mcp_args:
                            mcp_args["duration_ms"] = 10000
                        await self.execute_mcp_tool(tool, mcp_args)
                    elif tool in ["self.motor.turn_left", "self.motor.turn_right", "self.motor.spin_around", "self.motor.dance", "self.motor.happy", "self.motor.sad", "self.motor.wake_up"]:
                        await self.execute_mcp_tool(tool, {})
                    elif act == "stop":
                        await self.execute_mcp_tool("self.motor.stop", {})
                        self.stop_music()
                    else:
                        await self.execute_mcp_tool(tool, mcp_args)
                elif tool == "self.motor.turn_angle":
                    angle = args.get("angle_degrees", 90)
                    if angle == -45:
                        asyncio.create_task(send_robot_hardware_action("turn45_left", 0, 80))
                    elif angle == 45:
                        asyncio.create_task(send_robot_hardware_action("turn45_right", 0, 80))
                    elif angle == 180:
                        asyncio.create_task(send_robot_hardware_action("turn180", 0, 85))
                    elif angle < 0:
                        asyncio.create_task(send_robot_hardware_action("turn90_left", 0, 80))
                    else:
                        asyncio.create_task(send_robot_hardware_action("turn90_right", 0, 80))
                    await self.execute_mcp_tool("self.motor.turn_angle", args)
                elif tool == "self.robot.beep":
                    await self.execute_mcp_tool("self.robot.beep", args)
                elif tool == "self.camera.take_photo":
                    await self.execute_mcp_tool("self.camera.take_photo", {})
                elif "lamp" in tool:
                    await self.execute_mcp_tool(tool, {})
                elif "music.stop" in tool or "stop" in tool:
                    self.stop_music()

                # 2. Trả lời bằng giọng nói to, lịch sự (không tự ngắt bản nhạc đang phát)
                await self.send_tts_speech(reply, stop_music=False)
                return True
        return False

    async def handle_user_query(self, user_text: str):
        user_text = clean_repetitive_text(user_text)
        if not user_text:
            return
        logger.info(f"👤 Người dùng ra lệnh: '{user_text}'")
        text_lower = user_text.lower().strip()

        # Nếu đang phát nhạc, chỉ nhận các lệnh điều khiển rõ ràng hoặc từ khóa đánh thức,
        # tránh việc lời bài hát lọt vào micro bị hiểu nhầm thành câu hỏi tra cứu Google/LLM!
        if self.music_task and not self.music_task.done():
            is_valid_music_cmd = any(w in text_lower for w in [
                "dừng", "tắt", "ngừng", "thôi", "bài", "nhạc", "tiến", "lùi", "trái", "phải", "quay", "múa", "đèn", "hi", "ly", "chào"
            ])
            if not is_valid_music_cmd:
                logger.info(f"🎵 [MUSIC PLAYING] Bỏ qua âm thanh bài hát lọt vào micro: '{user_text}'")
                return

        # 0. KIỂM TRA LỆNH ĐÁNH THỨC "HI LY" / "HI" / "HẢI LÝ" / "HẢI LY"
        wake_patterns = [
            r'^(?:hi|hey|chào|halo|hello|alo|xin chào)\s*(?:ly|li|hải lý|hải ly|hải li|hai ly|hai li)?$',
            r'\b(?:hi ly|hải lý|hải ly|hai ly|hily|hey ly|chào ly)\b',
            r'^(?:hi|hello|xin chào|chào bạn|đánh thức|thức dậy đi|chào robot)$'
        ]
        if any(re.search(p, text_lower) for p in wake_patterns):
            wake_reply = "Xin chào bạn, mình là Robot trợ lý thông minh, mình sẵn sàng cùng với bạn!"
            logger.info("✨ [WAKE WORD] Đã kích hoạt câu lệnh đánh thức 'Hi Ly' / 'Hi'!")
            await self.execute_mcp_tool("self.motor.wake_up", {})
            asyncio.create_task(send_robot_hardware_action("wake_up", 0, 0))
            await self.send_tts_speech(wake_reply)
            return

        # 1. KIỂM TRA LỆNH DỪNG NHẠC / TẮT NHẠC
        if any(w in text_lower for w in ["dừng nhạc", "tắt nhạc", "ngừng nhạc", "thôi hát", "dừng bài"]):
            self.stop_music()
            await self.send_tts_speech("Dạ, em đã dừng phát nhạc rồi bạn nhé!")
            return

        # 2. KIỂM TRA LỆNH CỬ ĐỘNG NHANH (0ms phản xạ)
        has_fast_action = await self.execute_fast_command(user_text)
        if has_fast_action:
            return

        # 2.5 KIỂM TRA LỆNH CHUYỂN SANG LUYỆN TIẾNG ANH / IELTS HOẶC VỀ TIẾNG VIỆT
        if any(w in text_lower for w in ["luyện tiếng anh", "học tiếng anh", "nói tiếng anh", "luyện ielts", "học ielts", "speak english", "practice english", "tiếng anh đi", "practice ielts"]):
            self.preferred_lang = "en-US"
            en_welcome = (
                "Dạ được chứ ạ! Em rất sẵn lòng làm huấn luyện viên IELTS và luyện tiếng Anh cùng bạn. "
                "Từ bây giờ chúng mình cùng học từ vựng học thuật, rèn phản xạ và luyện Speaking nhé! "
                "Hello there! I am ready to be your dedicated IELTS coach. What topic or vocabulary would you like to practice today?"
            )
            await self.send_tts_speech(en_welcome)
            return

        if any(w in text_lower for w in ["chuyển về tiếng việt", "nói tiếng việt", "quay lại tiếng việt", "dùng tiếng việt", "tiếng việt đi", "switch to vietnamese"]):
            self.preferred_lang = "vi-VN"
            await self.send_tts_speech("Dạ, em đã chuyển về chế độ tiếng Việt rồi bạn nhé! Chúng ta tiếp tục tâm sự nào.")
            return

        # 3. KIỂM TRA LỆNH PHÁT BẤT KỲ BÀI HÁT NÀO TRÊN YOUTUBE
        music_patterns = [
            r'(?:bật|mở|phát|hát|nghe|cho nghe)\s+(?:bài|bài hát|nhạc|ca khúc|track)\s+(.+)',
            r'(?:bật|mở|phát|hát|cho nghe)\s+bài\s+(.+)',
            r'(?:bật|mở|phát|hát|cho nghe)\s+nhạc\s*(.*)',
            r'(?:nghe|play|music)\s+(.+)',
            r'nhạc\s+(.+)'
        ]
        is_music_request = False
        target_song = ""
        for pat in music_patterns:
            m = re.match(pat, text_lower)
            if m:
                is_music_request = True
                target_song = m.group(1).strip()
                break

        if is_music_request:
            if not target_song or target_song in ["lofi", "chill", "đi", "nào"]:
                target_song = "Nhạc Lofi Chill thư giãn nhẹ nhàng"
            await self.stream_youtube_music(target_song)
            return

        # 4. KIỂM TRA THỜI GIAN / NGÀY THÁNG HIỆN TẠI
        if any(w in text_lower for w in ["mấy giờ", "ngày mấy", "hôm nay ngày gì", "thời gian"]):
            dt_reply = get_current_datetime()
            await self.send_tts_speech(dt_reply)
            return

        # 5. KIỂM TRA CÂU HỎI THỜI TIẾT
        if any(w in text_lower for w in ["thời tiết", "mưa hay nắng", "nhiệt độ"]):
            weather_text = get_live_weather(user_text)
            await self.send_tts_speech(weather_text)
            return

        # 6. KIỂM TRA LỜI CHÀO & GIỚI THIỆU BẢN THÂN
        if any(w in text_lower for w in ["bạn là ai", "bạn tên gì", "giới thiệu", "làm được gì"]):
            intro_reply = (
                "Dạ em chào bạn ạ! Em là Xiaozhi - robot AI thông minh và thân thiện của bạn. "
                "Em có thể tìm kiếm mọi thông tin trên mạng như Google và AI để trả lời mọi thắc mắc, "
                "phát bất kỳ bài hát nào trên YouTube qua loa, và di chuyển, nhảy múa theo lệnh của bạn ạ!"
            )
            await self.send_tts_speech(intro_reply)
            return

        # 7. TÌM KIẾM MỌI THÔNG TIN DỮ LIỆU TRỰC TUYẾN (GOOGLE + WIKIPEDIA + AI)
        answer = await answer_user_query_online(user_text, self.conversation_history)
        await self.send_tts_speech(answer)

# ==================== 10. AIOHTTP ROUTERS & HANDLERS ====================
async def handle_ota(request):
    """Phản hồi kiểm tra OTA cho Robot (Hỗ trợ cả GET & POST, cả Local LAN và Cloud 24/7)"""
    global LAST_ROBOT_IP
    client_ip = request.remote
    if client_ip not in ["127.0.0.1", "localhost", LOCAL_IP]:
        LAST_ROBOT_IP = client_ip
    logger.info(f"📥 [OTA CHECK] Robot (IP: {client_ip}) vừa kiểm tra cấu hình OTA")
    
    # Tự động nhận diện URL WebSocket: hỗ trợ Cloud HTTPS (wss://) hoặc Local LAN (ws://)
    is_secure = request.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https"
    scheme = "wss" if is_secure else "ws"
    host_header = request.headers.get("X-Forwarded-Host", request.host)
    default_ws = f"{scheme}://{host_header}/ws" if ("." in host_header or ":" in host_header) else f"ws://{LOCAL_IP}:8000/ws"
    ws_target = os.environ.get("WS_URL", default_ws)

    response_data = {
        "server_time": {
            "timestamp": int(time.time() * 1000),
            "timezone_offset": 420
        },
        "websocket": {
            "url": ws_target,
            "version": 1
        }
    }
    return web.json_response(response_data)

async def handle_websocket(request):
    """Xử lý kết nối WebSocket thời gian thực với Robot"""
    global ACTIVE_SESSION, LAST_ROBOT_IP
    client_ip = request.remote
    if client_ip not in ["127.0.0.1", "localhost", LOCAL_IP]:
        LAST_ROBOT_IP = client_ip

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    session = XiaozhiSession(ws)
    ACTIVE_SESSION = session
    logger.info(f"🤖 [ROBOT CONNECTED] Robot Xiaozhi vừa kết nối thành công! Session ID: {session.session_id}")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")

                    if msg_type == "hello":
                        session.protocol_version = int(data.get("version", 1) or 1)
                        audio_params = data.get("audio_params", {})
                        sr_in = int(audio_params.get("sample_rate", 16000) or 16000)
                        ch_in = int(audio_params.get("channels", 1) or 1)
                        if sr_in != session.audio_decoder.sample_rate or ch_in != session.audio_decoder.channels:
                            session.audio_decoder = OpusStreamDecoder(sample_rate=sr_in, channels=ch_in)
                        logger.info(f"👋 Robot gửi gói Hello. Version: {session.protocol_version} | Audio: {audio_params}")
                        await session.send_json({
                            "type": "hello",
                            "transport": "websocket",
                            "session_id": session.session_id,
                            "audio_params": {
                                "format": "opus",
                                "sample_rate": 24000,
                                "channels": 1,
                                "frame_duration": 60
                            }
                        })
                        # Đặt âm lượng loa lên mức 100%
                        await session.execute_mcp_tool("self.audio_speaker.set_volume", {"volume": 100})

                    elif msg_type == "listen":
                        state = data.get("state")
                        text = data.get("text", "")
                        logger.info(f"🎙️ Robot Listen event: state={state}, text='{text}'")
                        if state == "start":
                            session.is_listening = True
                            session.last_audio_time = time.time()
                            session.echo_suppression_logged = False
                            session.audio_decoder.reset()
                        elif state == "stop":
                            session.is_listening = False
                            wav_data = session.audio_decoder.get_wav_data()
                            session.audio_decoder.reset()
                            if not session.is_processing:
                                async def _process_stop(w_data=wav_data, txt=text):
                                    session.is_processing = True
                                    try:
                                        if w_data:
                                            loop = asyncio.get_running_loop()
                                            recognized = await loop.run_in_executor(
                                                None, transcribe_audio_pcm, w_data, session.preferred_lang
                                            )
                                            if recognized:
                                                logger.info(f"🎤 [Micro Robot STT]: '{recognized}'")
                                                await session.handle_user_query(recognized)
                                                return
                                        if txt:
                                            await session.handle_user_query(txt)
                                    finally:
                                        session.audio_decoder.reset()
                                        session.is_processing = False
                                asyncio.create_task(_process_stop())
                        elif state == "detect" and text:
                            t_low = text.lower().strip()
                            wake_words_hw = ["你好小智", "小智", "nihao", "xiaozhi", "hi esp", "alexa", "hi ly", "hải lý"]
                            if any(w in t_low for w in wake_words_hw):
                                logger.info(f"✨ [WAKE WORD] Chip xe vừa nhận diện từ đánh thức: '{text}'")
                                session.audio_decoder.reset()
                                async def _wake_reply():
                                    session.is_processing = True
                                    try:
                                        await session.send_tts_speech("Dạ, em nghe ạ!", stop_music=False)
                                    finally:
                                        session.audio_decoder.reset()
                                        session.is_processing = False
                                asyncio.create_task(_wake_reply())
                            else:
                                asyncio.create_task(session.handle_user_query(text))

                    elif msg_type == "mcp":
                        logger.info(f"📥 [MCP Response từ Robot]: {data.get('payload')}")

                    elif msg_type == "abort":
                        logger.info("⏹️ Robot đã gửi tín hiệu ngắt (Abort/Dừng)")
                        session.stop_music()
                        session.is_speaking = False

                    elif msg_type == "text":
                        user_text = data.get("text", "")
                        if user_text:
                            asyncio.create_task(session.handle_user_query(user_text))

                except json.JSONDecodeError:
                    pass

            elif msg.type == web.WSMsgType.BINARY:
                # 1. TRIỆT TIÊU TIẾNG VỌNG LOA ngay khi nhận gói (tránh làm nhiễu bộ đếm nói)
                if session.is_speaking or (time.time() - session.last_speaking_end_time < 0.45):
                    if not session.echo_suppression_logged:
                        logger.info("🔇 [ECHO SUPPRESSION] Loa robot đang phát, tạm bỏ qua tiếng vọng micro.")
                        session.echo_suppression_logged = True
                    continue
                session.echo_suppression_logged = False

                if session.is_processing:
                    continue

                payload = msg.data
                if session.protocol_version == 2 and len(payload) > 16:
                    _ver, _mtype, _res, _ts, psize = struct.unpack(">HHIII", payload[:16])
                    if _mtype != 0:
                        continue
                    payload = payload[16:16 + psize]
                elif session.protocol_version == 3 and len(payload) > 4:
                    _mtype, _res, psize = struct.unpack(">BBH", payload[:4])
                    if _mtype != 0:
                        continue
                    payload = payload[4:4 + psize]

                session.is_listening = True
                session.last_audio_time = time.time()
                just_started = session.audio_decoder.add_packet(payload)
                if just_started:
                    logger.info("🎙️ [VAD] Đang nghe thấy giọng nói từ Micro Robot...")

            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"Lỗi WebSocket: {ws.exception()}")

    finally:
        session.stop_music()
        session.watcher_task.cancel()
        ACTIVE_SESSION = None
        logger.info("🔌 Robot đã ngắt kết nối WebSocket.")

    return ws

async def handle_api_action(request):
    """API điều khiển robot từ Web Dashboard"""
    global ACTIVE_SESSION, LAST_ROBOT_IP
    try:
        data = await request.json()
        command = data.get("command", "")
        if not command:
            return web.json_response({"status": "error", "message": "Thiếu tham số command"}, status=400)

        logger.info(f"🌐 [Lệnh từ Web/Micro]: '{command}'")

        # 1. Nếu robot đang kết nối WebSocket thời gian thực (hỗ trợ cả Render Cloud)
        if ACTIVE_SESSION is not None:
            await ACTIVE_SESSION.handle_user_query(command)
            return web.json_response({"status": "ok", "message": f"Đã gửi lệnh tới robot qua WebSocket: {command}"})

        # 2. Nếu xe chưa kết nối WebSocket, gửi bổ trợ qua HTTP REST nội bộ LAN (nếu có IP)
        cmd_lower = command.lower().strip()
        hardware_map = {
            "tiến": ("forward", 0, 85),
            "lùi": ("backward", 0, 85),
            "trái": ("left", 0, 80),
            "phải": ("right", 0, 80),
            "dừng": ("stop", 0, 0),
            "xoay": ("spin", 0, 85),
            "quay": ("spin", 0, 85),
            "múa": ("dance", 0, 0),
            "vui": ("happy", 0, 0),
            "buồn": ("sad", 0, 0),
            "thức dậy": ("wake_up", 0, 0),
        }
        for k, v in hardware_map.items():
            if k in cmd_lower:
                act, dur, spd = v
                asyncio.create_task(send_robot_hardware_action(act, dur, spd))
                break

        return web.json_response({
            "status": "ok", 
            "message": f"Đã kích hoạt điều khiển phần cứng tới xe ({LAST_ROBOT_IP}): {command}"
        })

    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def handle_logs(request):
    """API lấy nhật ký trực tiếp cho Web Dashboard"""
    global ACTIVE_SESSION, LAST_ROBOT_IP
    return web.json_response({
        "status": "ok",
        "connected": ACTIVE_SESSION is not None,
        "robot_ip": LAST_ROBOT_IP,
        "logs": list(LOG_BUFFER)
    })

async def handle_index(request):
    """Trang chủ hiển thị Camera Live, Trạng thái, Bảng nút, Micro giọng nói và Live Logs"""
    robot_status = "ĐÃ KẾT NỐI 🟢" if ACTIVE_SESSION else "XE SẴN SÀNG 🟡"
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Xiaozhi Voice & Car Dashboard</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: white; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 15px; box-sizing: border-box; }}
        .card {{ background: #1e293b; padding: 22px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); max-width: 640px; width: 100%; text-align: center; }}
        h1 {{ color: #38bdf8; font-size: 1.6rem; margin: 0 0 6px 0; }}
        .badge {{ background: {'#10b981' if ACTIVE_SESSION else '#f59e0b'}; color: white; padding: 5px 14px; border-radius: 20px; font-weight: bold; font-size: 0.88rem; display: inline-block; margin-bottom: 10px; }}
        .camera-box {{ position: relative; width: 100%; height: 240px; background: #000; border-radius: 14px; overflow: hidden; margin-bottom: 12px; border: 2px solid #334155; display: flex; align-items: center; justify-content: center; }}
        .camera-box img {{ width: 100%; height: 100%; object-fit: cover; }}
        .camera-badge {{ position: absolute; top: 10px; left: 10px; background: rgba(239,68,68,0.85); color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: bold; letter-spacing: 0.5px; }}
        .btn-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 10px; }}
        button {{ background: #3b82f6; color: white; border: none; padding: 12px 8px; border-radius: 10px; font-size: 0.95rem; font-weight: bold; cursor: pointer; transition: 0.15s; user-select: none; }}
        button:hover {{ background: #2563eb; transform: scale(1.02); }}
        button:active {{ transform: scale(0.96); }}
        .btn-mic {{ background: #ef4444; width: 100%; padding: 14px; font-size: 1.05rem; border-radius: 12px; margin-top: 10px; display: flex; align-items: center; justify-content: center; gap: 8px; }}
        .btn-mic:hover {{ background: #dc2626; }}
        .btn-mic.listening {{ background: #10b981; animation: pulse 1s infinite alternate; }}
        @keyframes pulse {{ from {{ transform: scale(1); }} to {{ transform: scale(1.03); }} }}
        .input-group {{ display: flex; gap: 8px; margin-top: 12px; }}
        input[type="text"] {{ flex: 1; padding: 10px 14px; border-radius: 10px; border: 1px solid #475569; background: #0f172a; color: white; font-size: 0.95rem; }}
        .terminal-box {{ background: #000; border: 1px solid #334155; border-radius: 10px; padding: 10px; margin-top: 10px; text-align: left; font-family: 'Consolas', monospace; font-size: 0.8rem; height: 150px; overflow-y: auto; color: #a7f3d0; }}
        .terminal-header {{ display: flex; justify-content: space-between; font-size: 0.78rem; color: #94a3b8; margin-top: 12px; margin-bottom: 4px; border-bottom: 1px solid #1e293b; padding-bottom: 4px; }}
        .guide-box {{ background: #0f172a; border-radius: 10px; padding: 10px; margin-top: 10px; text-align: left; font-size: 0.82rem; color: #cbd5e1; line-height: 1.5; border-left: 3px solid #38bdf8; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>🚗 Xiaozhi AI Voice & Car Dashboard</h1>
        <div id="statusBadge" class="badge">TRẠNG THÁI: {robot_status}</div>
        
        <!-- Live Camera Stream từ Robot -->
        <div class="camera-box">
            <span class="camera-badge">● LIVE CAMERA</span>
            <img id="camStream" src="" style="display:none; width:100%; border-radius:10px;" onerror="this.style.display='none'; document.getElementById('camPlaceholder').style.display='block';">
            <div id="camPlaceholder" style="padding: 22px 0;">
                <button onclick="toggleCam()" style="background:#334155; font-size:0.85rem; padding:8px 16px; border-radius:8px;">📹 Bật Camera Trực Tiếp</button>
            </div>
        </div>

        <!-- Nút micro nói chuyện trực tiếp -->
        <button id="micBtn" class="btn-mic" onclick="toggleSpeechRecognition()">
            🎤 BẤM ĐỂ NÓI RA LỆNH GIỌNG NÓI
        </button>

        <div style="margin-top: 14px;">
            <b style="color: #cbd5e1;">⚡ Bảng nút điều khiển tức thì (Độ trễ 0ms):</b>
            <div class="btn-grid">
                <button onclick="sendCommand('tiến lên')">▲ Tiến lên</button>
                <button onclick="sendCommand('lùi lại')">▼ Lùi lại</button>
                <button onclick="sendCommand('quay vòng')">🔄 Quay vòng</button>
                <button onclick="sendCommand('rẽ trái')">◀ Rẽ trái</button>
                <button onclick="sendCommand('dừng lại')" style="background:#ef4444;">⏹ Dừng lại</button>
                <button onclick="sendCommand('rẽ phải')">▶ Rẽ phải</button>
                <button onclick="sendCommand('nhảy múa đi')" style="background:#8b5cf6;">💃 Nhảy múa</button>
                <button onclick="sendCommand('bật đèn')" style="background:#f59e0b;">💡 Bật đèn</button>
                <button onclick="sendCommand('bật bài cắt đôi nỗi sầu')" style="background:#10b981;">🎵 Bật YouTube</button>
            </div>
        </div>

        <div class="input-group">
            <input type="text" id="customCmd" placeholder="Hỏi bất kỳ điều gì (VD: ai lên mặt trăng đầu tiên? Mở bài Sơn Tùng...)" onkeydown="if(event.key==='Enter') sendInput()">
            <button onclick="sendInput()">Gửi</button>
        </div>

        <div class="guide-box">
            💡 <b>Các tính năng nổi bật:</b><br>
            • <b>Tìm kiếm trực tuyến toàn diện:</b> Hỏi bất kỳ điều gì (giá vàng, thời tiết, lịch sử, ai là ai, định nghĩa...), robot sẽ tự động tìm kiếm trên mạng như Google và trả lời mượt mà.<br>
            • <b>Phát nhạc YouTube mọi bài hát:</b> Ra lệnh <i>"Bật bài [Tên bài hát]"</i> (VD: <i>"Bật bài Nơi này có anh"</i>, <i>"Mở bài Cắt đôi nỗi sầu"</i>), robot sẽ tự tìm và phát nhạc trực tiếp qua loa xe!<br>
            • <b>Điều khiển xe & Phản xạ siêu nhanh:</b> Nói lệnh hoặc bấm nút để xe tiến, lùi, rẽ, quay vòng, khiêu vũ và bật đèn!
        </div>

        <div>
            <div class="terminal-header">
                <span>📡 NHẬT KÝ THỜI GIAN THỰC (LIVE LOGS):</span>
                <span id="logCount">Đang cập nhật...</span>
            </div>
            <div id="logTerminal" class="terminal-box">Đang kết nối tới server...</div>
        </div>
    </div>

    <script>
        let recognition = null;
        let isRecording = false;

        const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (SpeechRec) {{
            recognition = new SpeechRec();
            recognition.lang = 'vi-VN';
            recognition.continuous = false;
            recognition.interimResults = false;

            recognition.onstart = () => {{
                isRecording = true;
                const btn = document.getElementById('micBtn');
                btn.className = 'btn-mic listening';
                btn.innerText = '🔴 ĐANG LẮNG NGHE... HÃY NÓI ĐI!';
            }};

            recognition.onresult = (e) => {{
                const transcript = e.results[0][0].transcript;
                document.getElementById('customCmd').value = transcript;
                sendCommand(transcript);
            }};

            recognition.onerror = (e) => {{
                console.warn('Lỗi micro:', e);
                if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {{
                    alert('Trình duyệt chưa được cấp quyền dùng Micro! Hãy bấm biểu tượng ổ khóa 🔒 cạnh thanh địa chỉ để Cho phép Micro.');
                }}
                stopMic();
            }};

            recognition.onend = () => {{
                stopMic();
            }};
        }}

        function stopMic() {{
            isRecording = false;
            const btn = document.getElementById('micBtn');
            btn.className = 'btn-mic';
            btn.innerText = '🎤 BẤM ĐỂ NÓI RA LỆNH GIỌNG NÓI';
        }}

        function toggleSpeechRecognition() {{
            if (!recognition) {{
                alert('Trình duyệt của bạn chưa hỗ trợ nhận diện giọng nói trực tiếp. Bạn hãy mở trang này trên Google Chrome hoặc Microsoft Edge nhé!');
                return;
            }}
            if (isRecording) {{
                recognition.stop();
            }} else {{
                recognition.start();
            }}
        }}

        async function sendCommand(cmd) {{
            try {{
                const res = await fetch('/api/action', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{command: cmd}})
                }});
                const result = await res.json();
                if (result.status !== 'ok') {{
                    console.log('Thông báo: ' + result.message);
                }}
            }} catch(e) {{
                console.warn('Lỗi kết nối server:', e);
            }}
        }}

        function sendInput() {{
            const input = document.getElementById('customCmd');
            if (input.value.trim()) {{
                sendCommand(input.value.trim());
                input.value = '';
            }}
        }}

        async function fetchLogs() {{
            try {{
                const res = await fetch('/api/logs');
                const data = await res.json();
                
                const badge = document.getElementById('statusBadge');
                if (data.connected) {{
                    badge.innerText = 'TRẠNG THÁI: KẾT NỐI THOẠI & ĐIỀU KHIỂN 🟢';
                    badge.style.background = '#10b981';
                }} else if (data.robot_ip) {{
                    badge.innerText = 'TRẠNG THÁI: XE SẴN SÀNG (' + data.robot_ip + ') 🟢';
                    badge.style.background = '#10b981';
                }} else {{
                    badge.innerText = 'TRẠNG THÁI: CHỜ KẾT NỐI 🟡';
                    badge.style.background = '#f59e0b';
                }}

                const term = document.getElementById('logTerminal');
                if (data.logs && data.logs.length > 0) {{
                    const wasAtBottom = term.scrollHeight - term.clientHeight <= term.scrollTop + 30;
                    term.innerHTML = data.logs.map(l => '<div>' + l + '</div>').join('');
                    document.getElementById('logCount').innerText = data.logs.length + ' bản ghi';
                    if (wasAtBottom) {{
                        term.scrollTop = term.scrollHeight;
                    }}
                }}
            }} catch(e) {{
                console.error(e);
            }}
        }}

        setInterval(fetchLogs, 1200);
        function toggleCam() {{
            var img = document.getElementById('camStream');
            var ph = document.getElementById('camPlaceholder');
            if (img.style.display === 'none') {{
                img.src = 'http://{LAST_ROBOT_IP}/stream';
                img.style.display = 'block';
                ph.style.display = 'none';
            }} else {{
                img.src = '';
                img.style.display = 'none';
                ph.style.display = 'block';
            }}
        }}

        fetchLogs();
    </script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

async def preload_voice_cache():
    """Tải trước các câu thoại cử động cơ bản vào RAM để phản hồi 0ms với âm lượng to"""
    common_replies = [
        "Dạ, em nghe ạ!",
        "Xin chào bạn, mình là Robot trợ lý thông minh, mình sẵn sàng cùng với bạn!",
        "Dạ em tiến lên ngay ạ!",
        "Dạ em lùi lại đây ạ!",
        "Dạ em rẽ trái ngay bạn nhé!",
        "Dạ em rẽ phải ngay bạn nhé!",
        "Dạ em đã dừng lại hoàn toàn rồi ạ!",
        "Dạ em đang quay vòng biểu diễn đây ạ!",
        "Dạ, cùng hòa nhịp khiêu vũ nào bạn ơi!",
        "Dạ em đã bật đèn pha chiếu sáng rồi ạ!",
        "Dạ em đã tắt đèn xe rồi bạn nhé!",
        "Dạ em đã dừng phát nhạc rồi bạn nhé!"
    ]
    logger.info("⚡ Đang nạp trước bộ đệm giọng nói âm lượng lớn vào RAM...")
    for text in common_replies:
        try:
            frames = await generate_opus_frames(text)
            if frames:
                VOICE_CACHE[text] = frames
        except Exception:
            pass
    logger.info(f"✅ Đã nạp thành công {len(VOICE_CACHE)} câu khẩu lệnh vào bộ nhớ tức thì 0ms.")

async def on_app_startup(app):
    asyncio.create_task(preload_voice_cache())

def create_app():
    app = web.Application()
    app.on_startup.append(on_app_startup)
    app.router.add_route("*", "/ota", handle_ota)
    app.router.add_route("*", "/xiaozhi/ota/", handle_ota)
    app.router.add_get("/ws", handle_websocket)
    app.router.add_post("/api/action", handle_api_action)
    app.router.add_get("/api/logs", handle_logs)
    app.router.add_get("/", handle_index)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", CONFIG["server"].get("port", 8000)))
    logger.info(f"=======================================================")
    logger.info(f"🚀 Xiaozhi AI Voice Server đang chạy tại: {LOCAL_IP}:{port}")
    logger.info(f"📌 OTA Config URL  : http://{LOCAL_IP}:{port}/ota")
    logger.info(f"📌 WebSocket URL   : ws://{LOCAL_IP}:{port}/ws")
    logger.info(f"📌 Web Dashboard   : http://{LOCAL_IP}:{port}/")
    logger.info(f"=======================================================")
    
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
