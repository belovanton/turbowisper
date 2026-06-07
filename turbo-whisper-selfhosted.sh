#!/bin/bash
# ╔═══════════════════════════════════════════════════════════════╗
# ║    🎙️  TURBO WHISPER + SELF-HOSTED SERVER  🎙️                 ║
# ║    Полный запуск: Whisper-сервер + Turbo Whisper GUI         ║
# ╚═══════════════════════════════════════════════════════════════╝

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
COMPOSE="$SCRIPT_DIR/docker-compose.yml"

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'
BOLD='\033[1m'

# ASCII-арт
echo -e "${MAGENTA}"
cat <<'BOOBS'
    (  )   (  )
   (o o)  (o o)
  (  V  )(  V  )
   \   /  \   /
    ~~      ~~
   WHISPER  WHISPER

      🎙️  TURBO WHISPER  🎙️
      Self-Hosted Edition
BOOBS
echo -e "${NC}"

# Проверяем Docker
echo -e "${CYAN}▶ Проверяем Docker...${NC}"
if ! docker info >/dev/null 2>&1; then
  echo -e "${RED}❌ Docker не запущен! Запусти Docker Desktop и попробуй снова.${NC}"
  exit 1
fi

# Проверяем venv
if [ ! -d "$VENV" ]; then
  echo -e "${RED}❌ Виртуальное окружение не найдено!${NC}"
  echo -e "   Запусти: cd $SCRIPT_DIR && python3 -m venv .venv${NC}"
  exit 1
fi

# Запускаем сервер
echo -e "${CYAN}▶ Запускаем Whisper-сервер...${NC}"
docker compose -f "$COMPOSE" up -d

# Ждём готовности сервера
echo -e "${CYAN}▶ Ждём готовности сервера (до 60 сек)...${NC}"
for i in {1..60}; do
  if curl -s http://localhost:8000/health >/dev/null 2>&1; then
    echo -e "${GREEN}✅ Сервер готов!${NC}"
    break
  fi
  sleep 1
  if [ $i -eq 60 ]; then
    echo -e "${YELLOW}⚠️  Сервер не ответил за 60 сек, но попробуем запустить GUI...${NC}"
  fi
done

# Показываем модель
echo -e "${CYAN}▶ Используемая модель:${NC}"
docker exec turbo-whisper-server env 2>/dev/null | grep WHISPER__MODEL || echo "  Systran/faster-whisper-small"

# Прогреваем модель тишиной — чтобы не грелась при первом нажатии
echo -e "${CYAN}▶ Прогрев модели...${NC}"
source "$VENV/bin/activate"
python3 - <<'WARMUP' 2>/dev/null && echo -e "${GREEN}✅ Модель прогрета и готова к работе!${NC}" || echo -e "${YELLOW}⚠️  Прогрев не удался, модель загрузится при первом запросе${NC}"
import httpx, wave, struct, io

sample_rate = 16000
buf = io.BytesIO()
with wave.open(buf, 'wb') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sample_rate)
    wf.writeframes(struct.pack('<' + 'h' * sample_rate, *([0] * sample_rate)))

r = httpx.post(
    'http://localhost:8000/v1/audio/transcriptions',
    files={'file': ('warmup.wav', buf.getvalue(), 'audio/wav')},
    data={'model': 'whisper-1', 'language': 'ru', 'response_format': 'json'},
    timeout=120.0
)
assert r.status_code == 200
WARMUP

echo ""
echo -e "${GREEN}${BOLD}🚀 Запускаем Turbo Whisper GUI...${NC}"
echo -e "${YELLOW}  Hotkey: Alt+Space (зажми и говори)${NC}"
echo -e "${YELLOW}  API:    http://localhost:8000${NC}"
echo ""

# Запускаем GUI из venv
source "$VENV/bin/activate"
exec turbo-whisper
