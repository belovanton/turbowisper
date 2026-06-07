#!/bin/bash
# ╔═══════════════════════════════════════════════════════════════╗
# ║     🎙️  TURBO-WHISPER SELF-HOSTED SERVER  🎙️                  ║
# ║     Локальный Whisper-сервер для голосовой диктовки          ║
# ╚═══════════════════════════════════════════════════════════════╝

set -e

COMPOSE_FILE="$(dirname "$0")/docker-compose.yml"
MODEL="${WHISPER_MODEL:-Systran/faster-whisper-small}"

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'
BOLD='\033[1m'

echo -e "${MAGENTA}"
cat <<'BOOBS'
    (  )  (  )
   (o o) (o o)
  (  V  )(  V  )
   \   /  \   /
    ~~      ~~
   WHISPER  WHISPER
BOOBS
echo -e "${NC}"

action="${1:-up}"

case "$action" in
  up|start)
    echo -e "${GREEN}${BOLD}▶ Запускаем Whisper-сервер...${NC}"
    echo -e "${CYAN}  Модель: ${MODEL}${NC}"
    echo -e "${CYAN}  URL:    http://localhost:8000${NC}"
    echo ""
    docker compose -f "$COMPOSE_FILE" up -d
    echo ""
    echo -e "${GREEN}${BOLD}✅ Сервер запущен!${NC}"
    echo -e "${YELLOW}  Проверь: curl http://localhost:8000/health${NC}"
    echo -e "${YELLOW}  Логи:    docker logs -f turbo-whisper-server${NC}"
    ;;
  down|stop)
    echo -e "${RED}${BOLD}▶ Останавливаем Whisper-сервер...${NC}"
    docker compose -f "$COMPOSE_FILE" down
    echo -e "${GREEN}✅ Сервер остановлен${NC}"
    ;;
  logs)
    echo -e "${CYAN}${BOLD}▶ Логи сервера:${NC}"
    docker logs -f turbo-whisper-server
    ;;
  status)
    echo -e "${CYAN}${BOLD}▶ Статус контейнера:${NC}"
    docker ps --filter name=turbo-whisper-server --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    ;;
  pull)
    echo -e "${YELLOW}${BOLD}▶ Обновляем образ...${NC}"
    docker compose -f "$COMPOSE_FILE" pull
    ;;
  *)
    echo -e "${BOLD}Использование:${NC} $0 {up|down|logs|status|pull}"
    echo ""
    echo -e "  ${GREEN}up${NC}     — запустить сервер"
    echo -e "  ${RED}down${NC}   — остановить сервер"
    echo -e "  ${CYAN}logs${NC}   — смотреть логи"
    echo -e "  ${CYAN}status${NC} — статус контейнера"
    echo -e "  ${YELLOW}pull${NC}   — обновить образ"
    exit 1
    ;;
esac
