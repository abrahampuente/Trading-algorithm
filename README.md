# algo-trading · Trading algorítmico y backtesting reproducible

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-8.0-4479A1?logo=mysql&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0%20%2B%20Alembic-D71F00)
![Alpaca](https://img.shields.io/badge/Broker-Alpaca%20Paper-FFD700)
![mypy](https://img.shields.io/badge/mypy-strict-2A6DB2)
![Tests](https://img.shields.io/badge/tests-530%2B-brightgreen)
![Estado](https://img.shields.io/badge/estado-v2%20paper%20trading-orange)

Sistema modular de trading algorítmico **Long** para activos líquidos de EE. UU., orientado a la investigación y al backtesting **reproducible**: ingesta de datos con trazabilidad completa, motor de backtesting diario con costes y gaps, validación por Walk-Forward Optimization y un daemon de ejecución en **Alpaca Paper** con reconciliación y telemetría.

> [!WARNING]
> El sistema **solo opera en paper trading**. Una guardia estricta impide enviar órdenes si la configuración, la clave o la cuenta no son de paper. Nada de este repositorio es recomendación de inversión.

---

## Tabla de contenidos

- [Estrategia](#estrategia)
- [Características](#características)
- [Resultados de la v1](#resultados-de-la-v1)
- [Arquitectura](#arquitectura)
- [Stack tecnológico](#stack-tecnológico)
- [Instalación](#instalación)
- [Uso](#uso)
- [Calidad](#calidad)
- [Roadmap](#roadmap)
- [Autor](#autor)

---

## Estrategia

Ruptura de volatilidad con momentum sobre velas diarias. La señal se genera al cierre de `t` y la entrada se ejecuta en la **apertura de `t+1`** (nunca al cierre de la vela generadora, para evitar *look-ahead*):

```text
Close[t] > Close[t-10] > Close[t-20]          # momentum
Close[t-1] <= UpperBand[t-1]                  # venía dentro de la banda
Close[t]   >  UpperBand[t]                    # rompe la banda superior de Bollinger
```

| Parámetro | Valor (v1 bloqueada) |
|---|---|
| Indicadores | Bollinger 20 (SMA 20, σ con `ddof=0`), ATR 20 Wilder congelado durante la operación |
| Stop Loss / Take Profit | entrada − 2 × ATR / entrada + 3 × ATR |
| Salida temporal | tras 20 sesiones de mercado |
| Riesgo por operación | 1 % del equity, máximo 20 % del equity por posición |
| Posiciones simultáneas | 5 |
| Escudo anti-gap | no entra si la apertura supera `Close + 0,5 × ATR` |
| Costes (perfil base) | 0,05 % comisión + 0,05 % slippage |
| Universo `diversified` | AAPL MSFT GOOGL QQQ SPY XOM JPM JNJ PG CAT GLD TLT XLE XLF XLV |

## Características

**Datos y trazabilidad**
- Ingesta diaria desde Yahoo Finance e intradía (1m/5m) desde Alpaca Market Data, con `Decimal`, validación OHLC y snapshots con checksum reproducible e idempotente.
- Capa *raw* y capa ajustada por splits separadas; dividendos acreditados en cash; calendario NYSE real (festivos y cierres tempranos).

**Backtesting e investigación**
- Motor diario con costes configurables (optimista / base / conservador), prioridad conservadora del SL, salidas temporales y persistencia de cada ejecución (`backtest_runs`, trades y curva de equity).
- **Resolución intrabarra**: cuando una vela toca SL y TP a la vez, descarga los minutos de esa sesión y resuelve el orden real.
- Barridos de parámetros y **Walk-Forward Optimization** (IS 36 / OOS 12 meses) con curva OOS encadenada y Eficiencia Walk-Forward.
- Motor de señales v2 multi-factor: fuerza relativa, rotación sectorial, régimen de volatilidad, VIX y tendencia del benchmark, con tests de invariancia a datos futuros.
- Informes en JSON y Markdown con métricas (retorno, MDD, Sharpe, Profit Factor, Win Rate, expectancy en R) y comparación con Buy & Hold.

**Ejecución en paper (v2)**
- Conector `AlpacaPaperBroker` con envío idempotente (`client_order_id` determinista), reintentos con backoff y registro de latencia por petición.
- Entradas `OPG` limitadas con escudo anti-gap, OCO de protección tras cada fill y salidas temporales.
- Daemon con tareas `pre_open`, `intraday` y `post_close`, parada segura con SIGINT/SIGTERM y reconciliación que **bloquea el envío** ante desajustes de inventario u órdenes desconocidas.
- Telemetría: slippage en puntos básicos, latencias p50/p95, tasa de ejecución y resumen diario por Discord o Telegram (opcional).

## Resultados de la v1

Universo diversificado, 2020-01-01 → 2026-10-22, perfil de costes base.

| Medición | Retorno | Max Drawdown | Sharpe | Profit Factor | Trades |
|---|---|---|---|---|---|
| Walk-Forward OOS (2023 → 2026) | +14,05 % | 5,49 % | 0,64 | 1,28 | 198 |
| Backtest de confirmación (dentro de muestra) | +26,29 % | 8,13 % | — | 1,21 | 332 |
| Buy & Hold diversificado | +184,19 % | 28,41 % | — | — | — |

La estrategia mantiene fuera de muestra una ventaja positiva y estable con drawdowns bajos, pero en este periodo alcista su retorno absoluto queda muy por debajo de Buy & Hold. Los resultados se documentan sin maquillaje: el detalle completo está en [`docs/project-status.md`](docs/project-status.md).

## Arquitectura

```text
src/algo_trading/
├── config/          # Settings con pydantic-settings (YAML + .env)
├── data/            # Proveedores (Yahoo, Alpaca, en memoria), validación, ajustes, calendario
├── domain/          # Acciones corporativas
├── indicators/      # SMA, Bollinger, ATR Wilder
├── strategy/        # Señal Long de la v1
├── signals/         # Motor v2: SignalGenerator, filtros de régimen, contexto sin look-ahead
├── portfolio/       # Position sizing, niveles SL/TP, asignación de capital
├── engine/          # Resolución intrabarra SL/TP
├── backtesting/     # Motor, servicio de ejecución y análisis de sensibilidad
├── analytics/       # Métricas, informes, walk-forward, comparación intrabarra
├── execution/       # Órdenes, broker Alpaca Paper, planificador, daemon, reconciliación
├── persistence/     # Modelos SQLAlchemy y repositorios (MySQL)
├── telemetry/       # Alertas, métricas operativas, notificadores
└── cli/             # algo-ingest, algo-backtest, algo-live
```

Principios de diseño: dominio separado de la infraestructura, extensión mediante `Protocol` y parámetros opcionales sin reescribir los módulos de la v1, y un **test de regresión** que exige que el backtest de la v1 reproduzca exactamente su checksum cuando las funciones v2 están desactivadas.

## Stack tecnológico

| Área | Herramientas |
|---|---|
| Lenguaje | Python 3.13 |
| Configuración | Pydantic, pydantic-settings, PyYAML |
| Persistencia | MySQL 8, SQLAlchemy 2, Alembic, PyMySQL |
| Datos de mercado | yfinance, Alpaca Market Data API v2, pandas-market-calendars |
| Broker | Alpaca Paper Trading API (librería estándar, transporte inyectable) |
| Calidad | pytest, Ruff, mypy `--strict` |
| Infraestructura | Docker Compose |

## Instalación

**Requisitos:** Python 3.13+, Docker (para MySQL) y, para datos intradía o paper trading, una cuenta gratuita de [Alpaca](https://alpaca.markets) en modo paper.

```bash
git clone https://github.com/abrahampuente/Trading-algorithm.git
cd Trading-algorithm

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env                 # Windows: copy .env.example .env
```

Configura `.env` (los secretos nunca van en `configs/` ni en git):

```dotenv
DATABASE_PASSWORD=algo-trading-dev-password   # debe coincidir con MYSQL_PASSWORD de docker-compose.yml
ALPACA_API_KEY_ID=PK...                       # opcional: intradía y paper trading
ALPACA_API_SECRET_KEY=...
ALPACA_PAPER=true                             # obligatorio para enviar órdenes
DISCORD_WEBHOOK_URL=                          # opcional
TELEGRAM_BOT_TOKEN=                           # opcional
TELEGRAM_CHAT_ID=                             # opcional
```

Levanta la base de datos y aplica las migraciones:

```bash
docker compose up -d
alembic upgrade head
```

Los parámetros de la estrategia, los universos, los perfiles de costes y la configuración del broker están en [`configs/environments/local.yaml`](configs/environments/local.yaml).

## Uso

### Ingesta de datos

```bash
algo-ingest                                  # universo por defecto (diversified) desde 2020
algo-ingest --universe macro_benchmarks      # SPY, QQQ, ^VIX y ETFs sectoriales para los filtros v2
algo-ingest --symbols AAPL MSFT --start 2022-01-01
```

### Backtesting

```bash
algo-backtest                                # valores bloqueados de la v1
algo-backtest --cost-profile conservador --no-persist
algo-backtest --use-sector-relative-strength --vix-filter-threshold 25
algo-backtest --intrabar-resolution          # resuelve conflictos SL/TP con minutos de Alpaca
algo-backtest --list-runs                    # ejecuciones guardadas

# Walk-Forward Optimization
algo-backtest --walk-forward --universe diversified \
  --sl-multiples 1.5 2.0 --tp-multiples 2.5 3.0 --exposures 0.10 0.15 0.20 \
  --sector-filter-grid off on --report-tag sector_filter
```

Los informes se escriben en `reports/` (JSON y Markdown).

### Paper trading

```bash
algo-live --check-only      # verificaciones de arranque y plan del día, sin bucle
algo-live --once            # un único ciclo del daemon
algo-live --plan-only       # genera y notifica el plan sin enviar entradas
algo-live                   # daemon continuo; Ctrl+C o SIGTERM = parada segura
```

Códigos de salida: `0` correcto, `1` verificación fallida, `2` desajuste de reconciliación.

## Calidad

```bash
pytest                      # tests unitarios; los de integración se omiten si MySQL no está disponible
ruff check . && ruff format --check .
mypy src
alembic check
```

Los proveedores y el broker usan un transporte HTTP inyectable, así que los tests no dependen de la red.

## Roadmap

| Hito | Contenido | Estado |
|---|---|---|
| v1 | Backtesting diario, walk-forward y valores bloqueados | ✅ Cerrada |
| v2 · Hito 1 | Motor de señales multi-factor y filtros de régimen | 🟡 Parcial (faltan Short, reversión a la media y volumen) |
| v2 · Hito 2 | Datos intradía y resolución intrabarra | ✅ Cerrado |
| v2 · Hito 3 | Broker Alpaca Paper y motor de órdenes | ✅ Cerrado |
| v2 · Hito 4 | Daemon, reconciliación, telemetría y notificaciones | ✅ Cerrado |
| v2 · Hito 5 | Paper trading en vivo durante 30–60 sesiones | ⏳ En curso |

## Autor

**Abraham Puente** — Backend & DevSecOps Developer · Ingeniería en Informática, Duoc UC
GitHub: [@abrahampuente](https://github.com/abrahampuente)

> Proyecto de investigación personal. Los resultados pasados no garantizan resultados futuros.
