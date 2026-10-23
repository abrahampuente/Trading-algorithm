# Estado del proyecto — Algo Trading

## Estado actual: v2 en diseño (iniciado 2026-10-23)

La especificación técnica de la v2 está en `docs/technical-spec-v2.md`. Cubre cuatro ejes: señales multi-factor (fuerza relativa sectorial, filtros de volumen y régimen, Short y reversión a la media), precisión intradía (resolución SL/TP con barras de 1m, correlación de cartera y costes dinámicos), paper trading en una cuenta demo de broker (Alpaca Paper, con órdenes, fills, latencia y slippage real) y telemetría con notificaciones.

Roadmap v2:

```text
Hito 1  Especificación v2 y motor de señales multi-factor          parcial (filtros de régimen; Short pendiente)
Hito 2  Proveedor intradía y simulación exacta intrabarra           validado con Alpaca real
Hito 3  Adaptador de broker en Sandbox (paper) y motor de órdenes   en curso
Hito 4  Scheduler diario/intradía, latencia y notificaciones        pendiente
Hito 5  Paper trading en vivo de 30-60 sesiones                     pendiente
```

Reglas de continuidad (sección 2 de la especificación v2): con las funciones v2 desactivadas, que es su valor por defecto, el backtest de la v1 debe reproducir exactamente su informe; los valores bloqueados de la v1 no cambian sin un nuevo walk-forward; los módulos v1 se amplían con `Protocol` y parámetros opcionales, sin reescribirlos.

### Hito 1 (en curso)

Regresión v1 (`tests/integration/test_v1_regression.py`, contra MySQL; se omite si no está disponible):

- Reejecuta sin persistir el backtest de confirmación con los valores por defecto de `local.yaml` (2020-01-01 → 2026-10-22) y exige el `report_checksum` del run `1fe790dc-6bf2-4c5c-aa41-35186937afc1` (`1e500e90…eed5c`) y sus métricas (equity final, retorno, MDD, Profit Factor, Win Rate, dividendos, 332 trades).
- Un split nuevo en el universo reescribe el histórico ajustado y cambia el checksum legítimamente: la referencia se regenera de forma explícita.

Paquete `signals/` (no lo usa todavía el motor de backtest; `strategy/` e `indicators/` no cambian):

- `Signal` (`side`, `strategy_id`, `close`, `atr`, `score`, `factors`), `Side` y `SignalGenerator` (`Protocol`).
- `SignalContext`: series de referencia (SPY, QQQ, ETFs sectoriales, `^VIX`); `until(t)` es una vista hasta `t` inclusive, la única que reciben los filtros.
- `MomentumBreakoutGenerator` (`momentum_breakout_v1`): envuelve `LongSignalGenerator` y emite exactamente sus señales con `score = momentum`.
- `CompositeSignalGenerator`: señal base + filtros AND; evalúa todos los filtros, añade sus valores a `factors` como `<filtro>.<valor>` y registra las señales bloqueadas (`FilteredSignal`).
- Filtros: `RelativeStrengthFilter` (RS frente a SPY/QQQ en `n` sesiones), `SectorRotationFilter` (ranking de ETFs sectoriales por RS, `top_k`), `VolatilityRegimeFilter` (percentil de `ATR / Close`), `VixRegimeFilter` y `BenchmarkTrendFilter` (benchmark sobre su SMA). Si faltan datos rechazan con motivo (`insufficient_history`, `missing_reference_data`, `unmapped_sector`).
- Tests: equivalencia con la v1, invariancia a barras futuras (símbolo y referencias) y detección de un filtro con fuga de datos.

Series de referencia (universo `macro_benchmarks` en `local.yaml`): `SPY QQQ ^VIX XLK XLF XLV XLE XLY XLP XLI XLC XLU XLB XLRE`, ingeridas con `algo-ingest --universe macro_benchmarks` (2026-10-23): 1711 sesiones por ETF y 1713 del `^VIX` (2020-01-02 → 2026-10-22; el VIX publica dos días sin sesión NYSE, irrelevante porque los filtros buscan el timestamp exacto). Splits SPDR de diciembre de 2025 en XLK, XLB, XLU y XLY.

Cableado al motor y a la CLI:

- `BacktestEngine.run(..., signal_generator=None, signal_context=None)`: con generador, las señales salen de él (con `CompositeSignalGenerator`, las bloqueadas quedan en `BacktestResult.filtered_signals`); excluyente con `precomputed_signals`. Mientras el motor siga trabajando con `LongSignal`, las señales deben ser Long con los factores de banda v1 (`long_signal_from_signal`; `momentum = score`).
- `BacktestRunRequest.reference_symbols` carga las series de referencia en `PreparedMarketData.reference_bars`; `BacktestRunService.run(..., signal_generator=...)` construye el `SignalContext`.
- Informe: `signal_filters` (estrategia, configuración de cada filtro, señales bloqueadas por filtro y motivos por falta de datos). Sin generador el campo no aparece en el JSON, así que el informe y el checksum v1 no cambian; con generador también se guarda en `backtest_runs.parameters.signal_filters` (sin migración).
- Configuración `signals` en `local.yaml` (`SignalSettings`): universo de referencia, benchmark SPY, `^VIX`, `relative_strength_lookback = 63`, `sector_top_k = 4`, los 11 ETFs sectoriales y el sector de cada símbolo de `diversified`. SPY, GLD y TLT no tienen sector y pasan el filtro sectorial (`sector_allow_unmapped`).
- `algo-backtest --use-sector-relative-strength` (rotación sectorial: el ETF del sector del símbolo debe estar entre los 4 de mayor RS frente a SPY a 63 sesiones) y `--vix-filter-threshold X` (cierre del VIX en `t` <= X). Sólo en backtests individuales; el nombre del informe lleva la etiqueta del filtro (`backtest_sector-rs_vix25_diversified_…`).

Comparación (diversified, valores v1, 2020-01-01 → 2026-10-22, perfil base, persistidas):

```text
Variante              Run        Retorno   MDD      PF    Trades  Win Rate  Exp. R  Exposición  Bloqueadas
v1 (sin filtros)      f0b61229   26.29 %   8.13 %   1.21  332     47.59 %   0.091   40.7 %      -
sector RS             2a9940cc   25.56 %   9.43 %   1.31  245     47.76 %   0.105   30.4 %      158
VIX <= 25             0e2b2ce8   15.78 %  10.07 %   1.15  298     46.64 %   0.077   36.8 %       70
VIX <= 20             bee3c7b9    3.57 %  13.09 %   1.05  230     45.65 %   0.037   28.1 %      176
sector RS + VIX <= 25 5e0654d7   14.97 %   9.74 %   1.21  221     46.15 %   0.083   27.4 %      207
Buy & Hold diversificado: 184.19 % (MDD 28.41 %)
```

- La ejecución sin filtros reproduce el checksum de la referencia v1 (`1e500e90…eed5c`).
- El filtro sectorial mejora la calidad por trade (PF 1,21 → 1,31, expectancy 0,091 → 0,105 R) con un retorno casi igual y menos exposición, pero empeora el MDD (8,1 % → 9,4 %). Bloquea 158 señales (5 por historial insuficiente al inicio de 2020).
- El filtro VIX empeora todo: las rupturas con VIX alto (rebotes tras correcciones) son de las mejores señales de la estrategia. Cuanto más estricto, peor.
- Es una comparación dentro de la muestra con parámetros de filtro no optimizados; antes de adoptar el filtro sectorial hay que validarlo con walk-forward (falta añadir los filtros a la rejilla).

### Filtro sectorial en walk-forward

- `ParameterGrid.use_sector_filter` (por defecto `(False,)`, la rejilla v1) es el eje más interno de la rejilla; `ParameterCombination` y `PeriodEvaluation` llevan `use_sector_filter`. Con `(False, True)` los empates IS se quedan con la señal sin filtro. El barrido (`--sweep`) no lo admite.
- `WalkForwardAnalyzer(..., reference_bars=..., sector_filter=...)`: las series de referencia llegan en la misma carga de MySQL que las barras (`BacktestRunService.prepare`, una sola sesión) y las señales filtradas se calculan una vez sobre todo el historial por parámetros de señal. Como el filtro sólo ve `SignalContext.until(t)`, valen para cualquier ventana (test de invariancia frente a un cálculo sobre datos truncados).
- Estudio por variante (`SectorFilterStudyResult`, informe `walk-forward-report-v4`): walk-forward restringido a sin filtro y a con filtro, más el optimizado en el que cada IS elige; frecuencia de selección (`sector_filter_selections`) y señales bloqueadas. Un test comprueba que cada variante coincide con un walk-forward ejecutado sólo con ese valor.
- CLI: `algo-backtest --walk-forward --sector-filter-grid off on` (sólo con `--walk-forward`).

Ejecución (diversified, IS 36 / OOS 12, Profit Factor, perfil base, 5 posiciones, 1 % de riesgo, SL 1,5 / 2,0, TP 2,5 / 3,0, exposición 10 / 15 / 20 %, filtro off / on: 24 combinaciones por ventana, 14 s):

```powershell
algo-backtest --walk-forward --universe diversified --sl-multiples 1.5 2.0 --tp-multiples 2.5 3.0 `
  --exposures 0.10 0.15 0.20 --sector-filter-grid off on --report-tag sector_filter
```

Informe: `reports/walk_forward_sector_filter_diversified_2020-01-01_2026-10-22_base_is36-oos12_profit_factor.md`.

```text
Variante               Retorno OOS  Anual.   MDD     PF    Sharpe  Exposición  Trades  WFE
Sin filtro (v1)        14.05 %      3.51 %   5.49 %  1.28  0.64    33.5 %      198     131.63 %
Con filtro sectorial    8.99 %      2.29 %   3.48 %  1.40  0.76    15.4 %      154     155.51 %
Optimizado (IS elige)  13.14 %      3.29 %   5.49 %  1.35  0.72    26.8 %      177     133.14 %
Buy & Hold diversificado OOS: 108.77 %

#  OOS   Elegido  PF IS sin / con  Retorno OOS sin / con  MDD OOS sin / con
1  2023  base     1.24 / 1.23      1.05 % / 1.49 %        5.49 % / 2.95 %
2  2024  base     1.21 / 1.20      7.07 % / 2.72 %        3.22 % / 1.50 %
3  2025  filtro   1.24 / 1.41      0.15 % / 0.11 %        2.57 % / 2.84 %
4  2026  filtro   1.14 / 1.22      5.25 % / 4.43 %        3.95 % / 1.22 %
```

Lectura:

- La variante sin filtro reproduce exactamente el walk-forward de referencia de la v1 (14,05 %, MDD 5,49 %, WFE 131,63 %).
- El filtro es robusto fuera de muestra (WFE 155 %) y mejora la calidad: PF 1,28 → 1,40, Sharpe 0,64 → 0,76 y MDD 5,5 % → 3,5 % (menor en 3 de 4 ventanas). Pero reduce el retorno OOS del 14,1 % al 9,0 % porque deja la mitad del capital ocioso (exposición media 33 % → 15 %). Retorno / MDD es prácticamente igual (2,56 frente a 2,58).
- La selección IS lo elige en 2 de 4 ventanas (2025 y 2026) y en las dos primeras queda casi empatado (PF IS 1,24 / 1,23 y 1,21 / 1,20): el IS no lo distingue con claridad. El optimizado queda entre ambos (13,1 %).
- Decisión: no se activa por defecto. Es una palanca de riesgo (menos drawdown y exposición), no de retorno. Su valor dependerá de reinvertir el capital liberado (topes de exposición mayores con filtro o más señales), que hay que validar con un nuevo walk-forward antes de usar el hold-out posterior al 2026-10-22.

Pendiente en el Hito 1: filtro sectorial con topes de exposición mayores; confirmación por volumen; Short y reversión a la media (requieren generalizar `EntryCandidate` más allá de `LongSignal`).

### Hito 2: datos intradía y resolución intrabarra SL / TP

Proveedor `AlpacaMarketDataProvider` (`data/providers/alpaca.py`), Alpaca Market Data API v2 (`GET /v2/stocks/bars`):

- `MarketDataProvider` de barras `1m` / `5m` (`Timeframe.ONE_MINUTE` / `FIVE_MINUTES`); `source = alpaca-iex` o `alpaca-sip` según el feed; timestamp = inicio de la barra en UTC; paginación con `next_page_token`.
- `adjustment = raw` por defecto (capa raw); `split` da la escala de `market_bars_adjusted` (`splits-v1`), la que usa la resolución intrabarra. No sirve acciones corporativas (llegan con el proveedor diario).
- JSON leído como `Decimal` (sin `float`), 8 decimales; barras con OHLC inconsistente descartadas en `rejected_bars`. Snapshot con checksum v1 y `snapshot_id = alpaca-<feed>:<tf>:<símbolos>:<inicio>:<fin>`.
- Cuotas y fallos: con `X-RateLimit-Remaining: 0` la siguiente petición espera a `X-RateLimit-Reset`; 429 espera `Retry-After` (o el reset, o backoff); 5xx y errores de red, backoff exponencial `1, 2, 4… s` hasta `max_delay` y `max_attempts` (5); otros 4xx fallan sin reintento (`AlpacaRequestError`); agotados los reintentos, `AlpacaUnavailableError`.
- Sólo librería estándar (`urllib`), transporte HTTP inyectable; tests con un transporte falso, sin red.
- `AlpacaSessionBarSource`: barras de 1 minuto de `[apertura, cierre)` de la sesión regular según `ExchangeCalendar` (incluidos cierres tempranos).
- Configuración `intraday` en `local.yaml` (`IntradaySettings`: feed `iex`, reintentos, tolerancia); credenciales `ALPACA_API_KEY_ID` y `ALPACA_API_SECRET_KEY` sólo en `.env` (añadidas a `.env.example`).

Resolución intrabarra (`engine/intrabar.py`, `IntrabarResolver`), sólo para posiciones Long:

1. La vela no toca SL ni TP: sin salida y sin descarga.
2. Toca sólo uno: ese nivel con las reglas de la v1 (`evaluate_bar_exit`), sin descarga.
3. Toca ambos y la apertura ya está más allá de un nivel (`OPENING_GAP`): SL a la apertura o TP al nivel, sin descarga.
4. Toca ambos dentro de la sesión: descarga una vez las barras de 1 minuto de esa sesión y recorre la trayectoria. El primer minuto que toca el SL decide `STOP_LOSS` a `min(apertura del minuto, SL)`; si el primero es el TP, `TAKE_PROFIT` al nivel sólo si el SL aparece más tarde en la trayectoria; un minuto con ambos niveles es SL (`SAME_MINUTE`).
5. Fallback de la v1 (SL): sin fuente, error de la fuente (cualquier excepción, cacheada por sesión), sesión sin barras, barras fuera del rango diario más allá de la tolerancia del 1 % (escala distinta, p. ej. sin ajustar por un split) o trayectoria que no muestra el orden (con IEX el SL diario puede no aparecer en los minutos).

Cada vela con ambos niveles deja un `IntrabarResolutionRecord` (resolución, salida, minuto del primer toque, error).

Integración:

- `BacktestEngine.run(..., intrabar_resolver=None)`: sin resolver, el paso 3 es el de la v1. `BacktestRunService.run(..., intrabar_resolver=...)`.
- Informe: `intrabar` (velas en conflicto, TP primero, SL primero, fallbacks, por resolución y registros), ausente del JSON sin resolver; también en `backtest_runs.parameters.intrabar_resolution` (sin los registros).
- `algo-backtest --intrabar-resolution`: proveedor Alpaca con `adjustment = split` y el calendario; falla si faltan las credenciales o el calendario. Sólo en backtests individuales; informe con la etiqueta `intrabar`.
- Regresión: sin `--intrabar-resolution` el checksum v1 no cambia (`test_v1_regression.py` en verde).

Validación con la API real (2026-10-23):

- Credenciales en `.env`: `ALPACA_API_KEY_ID`, `ALPACA_SECRET_KEY` (nombre habitual de Alpaca, aceptado como alias de `ALPACA_API_SECRET_KEY`) y `ALPACA_PAPER`. El primer par de claves devolvía 401 en `data.alpaca.markets` y `paper-api.alpaca.markets`; con las claves regeneradas la autenticación funciona.
- Ejecución: `algo-backtest --intrabar-resolution --stop-loss-atr 0.75 --take-profit-atr 1 --no-persist` (diversified, 2020-01-02 → 2026-10-22, feed IEX, `adjustment = split`). Informe `reports/intrabar_comparison_diversified_2020-01-01_2026-10-22.md`.

```text
Conflictos SL / TP                        5
  INTRADAY_PATH (minutos descargados)     3  → TP primero en los 3
  OPENING_GAP (sin descarga)              2  → SL a la apertura
  Fallback v1                             0

Sesión resuelta  Primer toque (UTC)  Minutos IEX  Salida
QQQ 2021-01-25   14:37               296          TP 330.10
AAPL 2023-06-05  14:08               390          TP 183.86
CAT 2026-02-04   14:32               343          TP 713.58

                    v1 (SL primero)   Intrabarra   Diferencia
Retorno acumulado   -16.33 %          -14.67 %     +1.66 pp
Maximum Drawdown     16.33 %           14.67 %     -1.66 pp
Win Rate             43.01 %           43.66 %     +0.65 pp
Profit Factor         0.78              0.80       +0.02
Expectancy R         -0.15             -0.13       +0.02
Salidas SL / TP      265 / 200         262 / 203
Trades distintos de la v1: 278 de 465 (efecto del cash en operaciones posteriores)

API Alpaca: 3 peticiones, 3 correctas, 0 errores 429 / 5xx / red / 4xx; latencia media 768 ms (máx. 980 ms);
cuota 200 peticiones/min, mínimo restante 199; sin esperas; 5,9 s en total (dos backtests y descargas).
```

Lectura:

- El adaptador funciona con datos reales: autenticación, paginación, sesión regular (incluidos minutos sin operaciones en IEX: 296-390 de 390) y escala ajustada coherente con las barras diarias (ninguna sesión cayó en `FALLBACK_INCONSISTENT`).
- Las tres sesiones resueltas con minutos alcanzaron el TP en los primeros minutos de la sesión, cuando la v1 las habría cerrado por SL. La mejora es real pero pequeña (+1,66 pp) y se da en una configuración no operable (pérdidas del 15-16 %), elegida sólo para provocar conflictos.
- Con los valores v1 bloqueados (SL 2 × ATR, TP 3 × ATR) no hay ningún conflicto: la resolución intrabarra no cambia la v1 y la regla conservadora no sesgó sus resultados.
- Los nombres de informe no incluyen SL / TP: una ejecución con niveles no estándar sobrescribe el informe con los valores por defecto (pendiente menor).

Detalles de la implementación tras la validación:

- Ante 401 / 403 el proveedor lanza `AlpacaAuthenticationError` y el resolver de la CLI la propaga (`fatal_errors`) en lugar de completar el backtest con fallback v1 en cada sesión.
- Telemetría por proveedor (`AlpacaRequestStats`): peticiones e intentos, 429 / 5xx / red / 4xx, latencia media y máxima, espera por cuota y backoff, `X-RateLimit-Limit` y mínimo de `X-RateLimit-Remaining`.
- `algo-backtest --intrabar-resolution` ejecuta ahora también el backtest con la regla v1 sobre los mismos datos (en memoria, sin persistir) y escribe `reports/intrabar_comparison_<etiqueta>.md|.json`: conflictos, TP primero / SL primero / fallback, impacto en retorno, MDD, Win Rate, Profit Factor, Expectancy y trades, trades que cambian y métricas de la API.

Conflictos SL / TP reales (diversified, 2020-01-01 → 2026-10-22, contados sin API con el resolver sin fuente):

```text
SL × ATR  TP × ATR  Trades  Conflictos  Necesitan datos intradía
2.0       3.0       332     0           0    (valores v1 bloqueados)
1.5       2.5       368     0           0
1.0       2.0       413     0           0    (especificación v1 original)
1.0       1.5       438     2           1    (1 OPENING_GAP)
0.75      1.0       465     5           3    (2 OPENING_GAP)
```

- Con los valores v1 la distancia SL–TP es de 5 × ATR y ninguna vela diaria la cubre: `--intrabar-resolution` no descarga nada y reproduce exactamente la v1 (26,29 %, MDD 8,13 %, Win Rate 47,59 %, PF 1,21; 0 peticiones a Alpaca). La regla conservadora de la v1 no ha sesgado los resultados publicados.
- La precedencia sólo importa con niveles por debajo de ≈2,5 × ATR de distancia total, fuera de la rejilla del walk-forward.

Hito 2 cerrado en cuanto a datos intradía y resolución intrabarra. Pendiente (pasa a trabajos posteriores): persistir las barras de 1 minuto descargadas como snapshots (`market_bars_raw`, timeframe `1m`); correlación de cartera y costes dinámicos (más relevantes para la v1 que la resolución intrabarra, a la vista de los conflictos).

### Hito 3 (en curso): conector de órdenes Alpaca Paper

Núcleo completado: conector `AlpacaPaperBroker`, guardia estricta de paper trading y `OrderPlanner`. Módulos nuevos en `execution/` (todavía no los usa ningún pipeline ni la CLI; verificados sólo con respuestas simuladas, sin órdenes reales enviadas):

- `orders.py`: `OrderRequest` (validada: limitada con precio, stop con stop, mercado sin precios, `OPG` sólo mercado o limitada, OCO = venta limitada con TP > SL y `day` / `gtc`), `OrderState` (con patas `legs`), `OrderStatus` normalizado (`SUBMITTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED`, `REJECTED`, `REPLACED`) a partir de los estados de Alpaca (los transitorios como `pending_new`, `accepted`, `pending_cancel` o `done_for_day` cuentan como `SUBMITTED`), `validate_transition` y `OrderBook`, registro local que rechaza transiciones imposibles (p. ej. `FILLED → SUBMITTED`) o cantidades ejecutadas que bajan, y emite un `OrderEvent` por cambio. `round_to_tick` aplica los ticks de Alpaca (0,01 desde 1 USD, 0,0001 por debajo) con redondeo explícito.
- `broker.py`: contrato `BrokerAdapter` (cuenta, posiciones, enviar, consultar por `client_order_id`, órdenes abiertas, cancelar), `AccountSnapshot`, `BrokerPosition`.
- `alpaca_broker.py`: `AlpacaPaperBroker` (`/v2/account`, `/v2/positions`, `/v2/orders`, `/v2/orders:by_client_order_id`, `DELETE /v2/orders/{id}`), sólo librería estándar con transporte inyectable.
- `order_planner.py`: `OrderPlanner` de `PlannedEntry` (salida del `PortfolioAllocator`) a órdenes:
  - Entrada: compra limitada `OPG` con `límite = Close[t] + 0,5 × ATR[t]` redondeado hacia abajo: el escudo anti-gap de la v1 lo aplica el broker (si la apertura supera el límite, la orden no se ejecuta y termina `CANCELED` / `EXPIRED`, el equivalente a `GAP_FILTERED`).
  - SL / TP: OCO `GTC` tras la ejecución de la entrada (Alpaca no admite bracket con `OPG`), con los niveles recalculados sobre el precio medio ejecutado y el ATR congelado de la señal; SL y TP redondeados hacia arriba (stop algo más alto: pérdida nunca mayor que la planificada).
  - Salida temporal: venta a mercado `OPG`.
  - `client_order_id = algo-<intención>-<símbolo>-<AAAAMMDD>-<hash>`, determinista: reenviar el plan de una sesión no duplica órdenes.

Seguridad (`PaperTradingGuard`), comprobada al crear el broker y antes de cada orden o cancelación; si falla, `LiveTradingNotAllowedError` y no se envía nada:

1. `ALPACA_PAPER` con el valor exacto `true` (sin distinguir mayúsculas). Ausente, vacío, `false`, `1`, `yes`… son fatales. Se lee como texto para que pydantic no acepte `1` o `yes` como verdadero.
2. URL base exactamente `https://paper-api.alpaca.markets` (también fijada por tipo en `BrokerSettings`).
3. Clave de paper (`PK…`).
4. Cuenta de paper (`account_number` `PA…`), verificada con `GET /v2/account` antes de la primera orden.
5. 401 / 403 del broker también son fatales.

Envío idempotente: ante error de red o 5xx en el `POST` se consulta la orden por `client_order_id` antes de reenviar (la orden pudo llegar); 429 se reintenta con backoff; 4xx (p. ej. 422 por poder de compra) se rechaza sin reintento.

Configuración: sección `broker` en `local.yaml` (`BrokerSettings`) y `ALPACA_PAPER=true` en `.env` (añadido a `.env.example`); `build_alpaca_paper_broker(settings)`.

Pendiente en el Hito 3: persistencia de órdenes, eventos y fills (migración); reconciliación con las posiciones del broker; primer ciclo manual en la cuenta paper (envío, fill, OCO, cancelación).


## v1 cerrada (2026-10-23)

La v1 (backtesting diario, sin órdenes reales) queda cerrada con estos valores por defecto, definidos en `configs/environments/local.yaml` y usados por `algo-ingest` y `algo-backtest` cuando no se indican argumentos:

```text
Universo por defecto        diversified (15 activos): AAPL MSFT GOOGL QQQ SPY XOM JPM JNJ PG CAT GLD TLT XLE XLF XLV
max_positions               5
risk_per_trade              0.01  (1,0 % del equity)
stop_loss_atr_multiple      2.0   (SL = entrada - 2 × ATR)
take_profit_atr_multiple    3.0   (TP = entrada + 3 × ATR)
max_position_exposure       0.20  (20 % del equity por posición)
Perfil de costes            base (0,05 % comisión + 0,05 % slippage)
Calendario / dividendos     NYSE / acreditados en cash
```

Validación por Walk-Forward Optimization (IS 36 meses / OOS 12 meses, OOS 2023-01-01 → 2026-10-22, universo diversificado):

- WFE entre 89,8 % (rejilla SL × TP × exposición, 5 posiciones y 0,5 % de riesgo) y 132 % (nivel 5 posiciones / 1 % de riesgo del estudio de escalado).
- **Resultado OOS de referencia** (walk-forward con 5 posiciones y 1 % de riesgo): **+14,05 % de retorno acumulado, MDD 5,49 %, Sharpe 0,64**. En ese walk-forward el SL, el TP y la exposición se re-optimizaban en cada ventana; eligió SL 2,0 y TP 3,0 en tres de cuatro ventanas, con exposición 15 %, 20 %, 10 % y 20 %.
- El conjunto fijo bloqueado, aplicado sin re-optimizar al mismo periodo OOS, da +12,44 %, MDD 6,88 % y Sharpe 0,49. No es estrictamente fuera de muestra: los valores se eligieron tras ver los resultados del walk-forward.
- Backtest de confirmación con los valores por defecto (`algo-backtest`, 2020-01-02 → 2026-10-22, run `1fe790dc-6bf2-4c5c-aa41-35186937afc1` en `backtest_runs`): +26,29 %, MDD 8,13 %, Profit Factor 1,21, 332 trades, Win Rate 47,6 %, tiempo en mercado 81 %, exposición media 40,7 %, dividendos 5.720,64. Buy & Hold diversificado del mismo periodo: +184,19 % (MDD 28,41 %). Este periodo incluye los datos con los que se eligieron los parámetros: no es una medida fuera de muestra.

Notas sobre los valores bloqueados:

- 5 posiciones × 1 % de riesgo implican un riesgo teórico simultáneo del 5 %, por encima del 2,5 % de la especificación v1 original (decisión explícita al cerrar la v1).
- Los valores por defecto de dominio (`RiskParameters`: 0,5 %, SL 1×, TP 2×, 10 %) siguen siendo los de la especificación original; los valores de la v1 se aplican desde la configuración (`ApplicationSettings.strategy`, `default_universe`). Cualquier argumento explícito de la CLI los sustituye.
- La estrategia conserva fuera de muestra una ventaja positiva y estable con drawdowns bajos, pero su retorno absoluto queda muy por debajo de Buy & Hold en este periodo alcista.

## 1. Propósito

Construir un sistema modular de trading algorítmico Long para activos líquidos, orientado a investigación y backtesting reproducible.

La estrategia utiliza:

- Momentum.
- Ruptura de volatilidad mediante Bandas de Bollinger.
- Gestión de riesgo mediante ATR.
- Ejecución simulada con datos diarios e intradía.
- Persistencia relacional en MySQL.
- Trazabilidad completa de datos, configuración, costes y resultados.

La v1 no ejecutará órdenes reales.

---

## 2. Especificación de la estrategia

### Entrada

La señal se genera al cierre de la vela diaria `t` cuando se cumplen simultáneamente:

```text
Close[t] > Close[t-10] > Close[t-20]
Close[t-1] <= UpperBand[t-1]
Close[t] > UpperBand[t]
```

La señal se genera al cierre de `t`, pero la entrada se simula en la apertura de `t+1`.

No se utiliza el cierre de la vela generadora como precio de entrada.

### Indicadores

- Bollinger: ventana de 20 días.
- Media: SMA de 20 días.
- Desviación estándar: `ddof=0`.
- ATR: 20 días.
- ATR: suavizado de Wilder/RMA.
- El ATR queda congelado durante toda la operación.
- Warm-up mínimo: 21 velas operativas consecutivas.

### Salidas

Para una posición Long:

```text
Stop Loss = precio_entrada - ATR_entrada
Take Profit = precio_entrada + 2 * ATR_entrada
```

También existe salida temporal después de 20 días de mercado.

La salida temporal se ejecutará en la apertura de la sesión definida por la convención temporal final. Esta convención debe quedar cubierta por pruebas antes de implementar el motor.

No existe trailing stop en la v1.

---

## 3. Gestión monetaria

Parámetros iniciales:

```text
Riesgo por operación: 0,5 % del equity
Máximo de posiciones: 5
Máximo una posición por símbolo
Exposición nominal máxima por posición: 10 % del equity
Apalancamiento: no permitido
```

Cálculo:

```text
riesgo_monetario = equity * 0.005
riesgo_por_unidad = precio_entrada - stop_loss
cantidad_por_riesgo =
    floor(riesgo_monetario / riesgo_por_unidad)
cantidad_final =
    min(
        cantidad_por_riesgo,
        cantidad_por_capital,
        cantidad_por_exposicion
    )
```

Si la cantidad final es menor que una unidad, la operación no se abre.

El sizing utiliza equity:

```text
equity = cash + market_value
```

Cuando haya señales simultáneas y no existan suficientes plazas, se prioriza por:

1. Fuerza del momentum.
2. Ticker alfabéticamente, como desempate determinista.

---

## 4. Ejecución

### Slippage y costes

Los costes son configuración externa:

```text
Comisión base: 0,05 %
Slippage base: 0,05 %
```

Perfiles definidos en `configs/environments/local.yaml` (`cost_profiles`) y seleccionables con `algo-backtest --cost-profile`:

```text
Perfil        Comisión   Slippage
optimista     0,02 %     0,02 %
base          0,05 %     0,05 %   (por defecto)
conservador   0,10 %     0,10 %
```

En compras:

```text
precio_ejecutado = precio_teórico + slippage_adverso
```

En ventas:

```text
precio_ejecutado = precio_teórico - slippage_adverso
```

Cada trade debe registrar:

- Precio teórico.
- Precio ejecutado.
- Cantidad.
- Costes.
- PnL bruto.
- PnL neto.
- Motivo de salida.
- ATR de entrada.
- SL.
- TP.
- Riesgo planificado.

### Gaps

Existe un filtro anti-gap configurable. Si la apertura de `t+1` supera el cierre de la señal por encima del umbral configurado, se cancela la entrada y se registra:

```text
GAP_FILTERED
```

El ATR utilizado para el filtro es el calculado al cierre de `t`.

Los gaps contra SL o TP se ejecutan al precio disponible de apertura cuando sea peor que el nivel solicitado.

### Conflicto SL/TP

Si una barra intradía alcanza SL y TP y no existe granularidad suficiente para conocer el orden, se aplica una política conservadora:

```text
El SL tiene prioridad sobre el TP.
```

---

## 5. Convención de precios

Las señales e indicadores se calculan con precios ajustados.

Se mantienen dos capas:

```text
market_bars_raw
market_bars_adjusted
```

Los datos raw son inmutables.

Los ajustes corporativos se almacenan en:

```text
corporate_actions
```

Los splits ajustan históricamente:

- Open.
- High.
- Low.
- Close.
- Volume.

Los dividendos no ajustan precios en el flujo actual. Se utilizarán posteriormente para acreditar efectivo al cash en la fecha ex-dividendo.

### Splits

Convención:

```text
ratio = 2.0  → split 2:1
ratio = 0.5  → reverse split 1:2
factor = 1 / ratio
```

Para una barra anterior al `ex_date`:

```text
precio_ajustado = precio_raw * factor
volumen_ajustado = volumen_raw / factor
```

Los factores de múltiples splits se multiplican acumulativamente.

---

## 6. Arquitectura actual

```text
src/algo_trading/
├── config/
├── data/
│   ├── adjustments/
│   ├── providers/
│   └── validation/
├── domain/
│   └── corporate_actions/
├── persistence/
│   ├── models/
│   └── repositories/
├── indicators/
├── strategy/
├── execution/
├── portfolio/
├── backtesting/
└── analytics/
```

---

## 7. Estado completado

### Infraestructura

- Proyecto Python creado con src layout.
- Entorno virtual `.venv`.
- VS Code configurado para activar `.venv`.
- Git inicializado en `main`.
- Repositorio remoto GitHub configurado.
- Commits pequeños y descriptivos.
- Python 3.13.3.
- MySQL 8.0 ejecutándose mediante Docker.
- Docker Compose configurado.
- SQLAlchemy 2.x.
- Alembic.
- PyMySQL.
- Pydantic 2.x.
- PyYAML.
- Pytest.
- Ruff.
- Mypy.

### Configuración

- Configuración tipada con `ApplicationSettings`.
- Configuración no sensible en YAML.
- Secretos locales en `.env`.
- `.env` excluido de Git.
- `pyproject.toml` configurado.

Validaciones activas:

```powershell
pytest
ruff check .
ruff format --check .
mypy src
```

### Persistencia

Migraciones aplicadas:

```text
market_bars_raw
corporate_actions
market_bars_adjusted
data_snapshots
```

Restricciones relevantes:

```text
UNIQUE(symbol, timestamp, timeframe, source)
```

para raw.

```text
UNIQUE(symbol, timestamp, timeframe, source, adjustment_version)
```

para adjusted.

### Datos raw

Implementado:

- DTO `MarketBar`.
- Validación OHLCV con Pydantic.
- Rechazo de precios no positivos.
- Rechazo de volumen negativo.
- Rechazo de OHLC inconsistente.
- Detección de barras duplicadas.
- Ordenamiento por símbolo y timestamp.
- `RawMarketBarRepository`.
- `IngestionService`.
- Escritura transaccional.
- Rollback ante errores.
- Prueba de integración contra MySQL.

### Ajustes corporativos

Implementado:

- DTO `CorporateSplit`.
- DTO `CorporateDividend`.
- `CorporateActionMapper`.
- `SplitAdjustmentCalculator`.
- Factores acumulativos para múltiples splits.
- `AdjustedMarketBarBuilder`.
- `AdjustedMarketBarRepository`.
- `AdjustedMarketDataService`.
- Repositorio de lectura de acciones corporativas.
- Método de lectura que devuelve `CorporateSplit`.
- Repositorio de lectura de barras raw.
- Repositorio de lectura de barras adjusted.

### Proveedores de datos y snapshots

Implementado:

- Contrato `MarketDataProvider` mediante `Protocol`.
- DTOs `DataRequest`, `CorporateActionRequest`, `DataSnapshot` y `MarketDataSnapshot`.
- `InMemoryMarketDataProvider` para pruebas.
- Validación interna de snapshots: conteos, `source`, `timeframe` y duplicados.
- Tabla `data_snapshots` y repositorio de escritura/consulta.
- Repositorio de escritura para `CorporateAction`.
- `MarketDataSnapshotPersistenceService` con persistencia atómica de snapshot, barras raw y acciones corporativas.
- `MarketDataSnapshotIngestionService` que obtiene, valida y persiste snapshots desde un proveedor.
- Trazabilidad mediante `snapshot_id` opcional en `market_bars_raw` y `corporate_actions`.
- Consultas de barras raw y acciones corporativas por `snapshot_id`.
- Checksum reproducible de snapshots (`calculate_snapshot_checksum`, versión `market-data-snapshot-v1`).
- Verificación del checksum declarado y política de idempotencia por `snapshot_id`.
- Lectura de dividendos como DTO `CorporateDividend` (`get_dividend_dtos_by_symbol`).
- Pruebas unitarias e integraciones contra MySQL para proveedor, snapshots, persistencia, rollback y trazabilidad.

### Convención de checksum e idempotencia de snapshots

Algoritmo (`src/algo_trading/data/providers/snapshot_checksum.py`):

```text
checksum = sha256(json_canónico(payload)).hexdigest()
```

- `payload = {version, bars, splits, dividends}`.
- Barras ordenadas por `(symbol, timestamp, timeframe, source)`; splits y dividendos por `(symbol, ex_date, source)`. El orden de entrega del proveedor no afecta al resultado.
- JSON con `sort_keys=True`, separadores `(",", ":")` y `ensure_ascii=True`.
- `Decimal` normalizado (`Decimal("103.0")` y `Decimal("103.00000000")` producen el mismo hash).
- Timestamps serializados en UTC con microsegundos y offset `+00:00`. Los timestamps naive se interpretan como UTC, de modo que el checksum recalculado desde MySQL coincide con el original.
- Fechas `ex_date` en ISO `YYYY-MM-DD`.
- Los metadatos (`snapshot_id`, `retrieved_at`, conteos) no forman parte del hash: el checksum identifica contenido, no la descarga.
- Cualquier cambio en la serialización exige una nueva versión de `CHECKSUM_VERSION`. Un test fija el hash de referencia de v1.

Idempotencia (`MarketDataSnapshotPersistenceService.persist`):

1. Se recalcula el checksum sobre el contenido. Si no coincide con `metadata.checksum`, se lanza `SnapshotChecksumMismatchError` y no se escribe nada.
2. Si el `snapshot_id` no existe, se persisten snapshot, barras y acciones corporativas en una única transacción.
3. Si el `snapshot_id` existe con el mismo checksum, `source` y `timeframe`, la operación es un no-op y devuelve los metadatos existentes.
4. Si el `snapshot_id` existe con otro checksum, `source` o `timeframe`, se lanza `SnapshotContentConflictError`.
5. Si el mismo contenido llega con otro `snapshot_id`, las restricciones UNIQUE de raw y corporate actions lo rechazan (`DuplicateMarketBarError` / `DuplicateCorporateActionError`).

`snapshot_id` debe identificar de forma estable la solicitud. `InMemoryMarketDataProvider` usa `source:timeframe:start:end`; el proveedor real debe documentar su convención.

### Indicadores y señales (Hito 1)

Implementado en `Decimal` puro, sin pandas, como funciones sin efectos secundarios:

- `PriceBar` (`Protocol`): lo cumplen `MarketBar` y `MarketBarAdjusted`. Los indicadores se calculan sobre precios ajustados.
- Las series deben ser de un único símbolo y con timestamps estrictamente crecientes.
- Contexto `Decimal` explícito: precisión 28, `ROUND_HALF_EVEN`. No se redondea a ticks; la política de redondeo sigue aplazada.
- `simple_moving_average`: SMA móvil; `None` durante el warm-up.
- `rolling_population_std` y `bollinger_bands`: desviación estándar poblacional (`ddof=0`), bandas `SMA ± k·σ` con `k = 2` (confirmado; configurable).
- `true_ranges`: `max(H-L, |H-C[t-1]|, |L-C[t-1]|)`; la primera barra no tiene TR.
- `wilder_atr`: semilla `media(TR[1..20])` en el índice 20 y luego `ATR[t] = (ATR[t-1]·19 + TR[t]) / 20`. El primer ATR está disponible con 21 barras, coherente con el warm-up mínimo.
- `calculate_indicators` devuelve una tupla de `IndicatorRow` (equivalente a un DataFrame) con `sma`, `upper_band`, `lower_band` y `atr`.
- `LongSignalGenerator` emite `LongSignal` al cierre de `t` cuando `Close[t] > Close[t-10] > Close[t-20]`, `Close[t-1] <= UpperBand[t-1]` y `Close[t] > UpperBand[t]`. La señal incluye el ATR de `t` (para el filtro anti-gap y el SL/TP) y `momentum = Close[t] / Close[t-20] - 1` (confirmado) para priorizar señales simultáneas.

Garantía de no look-ahead:

- Cada indicador en `t` sólo lee barras `<= t`.
- `LongSignalGenerator.evaluate` recibe únicamente el historial hasta `t`; no tiene acceso a `t+1`.
- La señal no contiene precio de entrada: la entrada se simulará en la apertura de `t+1` en el motor de ejecución.
- Tests: los indicadores y señales calculados sobre cualquier prefijo coinciden con los de la serie completa, y alterar barras posteriores a una señal no la modifica. Ambos tests detectan un sesgo inyectado artificialmente.

Conteo de sesiones: los indicadores cuentan barras. Con calendario de mercado (ver «Calendario de mercado»), el backtest exige que cada serie cubra todas sus sesiones, de modo que `t-10`, `t-20` y las ventanas de 20 equivalen exactamente a sesiones de mercado.

### Gestión monetaria y portfolio (Hito 2)

`PositionSizer` (`RiskParameters`: riesgo 0,5 %, exposición 10 %, 5 posiciones, comisión configurable, por defecto 0):

```text
cantidad_por_riesgo     = floor(equity * 0.005 / (entrada - SL))
cantidad_por_capital    = floor(cash / (entrada * (1 + comisión)))
cantidad_por_exposicion = floor(equity * 0.10 / entrada)
cantidad_final          = min(las tres)
```

- Todas las cantidades se redondean hacia abajo a unidades enteras (`ROUND_FLOOR`).
- Si `cantidad_final < 1`, el resultado no es operable y la entrada se rechaza.
- `PositionSizingResult` registra las tres cantidades, el factor limitante (`risk`, `capital`, `exposure`; en empate se informa en ese orden), el riesgo planificado, el nocional y la comisión.
- La comisión se incluye en el límite de capital para que la compra nunca supere el cash.
- `calculate_long_exit_levels`: `SL = entrada - ATR`, `TP = entrada + 2 * ATR`, con el ATR de la señal congelado.

`PortfolioState`: estado inmutable con `cash` y posiciones; `equity = cash + valor de mercado`, como máximo una posición por símbolo.

`PortfolioAllocator.allocate(candidatos, estado)`:

1. Ordena los `EntryCandidate` por mayor `momentum` y, en empate, por ticker alfabético.
2. Rechaza `ALREADY_OPEN` si el símbolo ya tiene posición.
3. Rechaza `MAX_POSITIONS_REACHED` si ya hay 5 posiciones.
4. Dimensiona la entrada; si la cantidad es < 1, rechaza `BELOW_MINIMUM_QUANTITY` y sigue con el siguiente candidato.
5. Descuenta el coste total del cash y añade la posición.

Decisiones:

- `EntryCandidate.entry_price` es el precio esperado de ejecución en la apertura de `t+1` (incluido el slippage). Lo aporta el motor de ejecución, que también aplica antes el filtro anti-gap.
- El equity se fija al inicio de cada lote de señales simultáneas; el cash se reduce tras cada entrada aprobada. Así el tamaño no depende del orden de compra salvo por el cash disponible.
- Dos candidatos del mismo símbolo en un lote son un error de entrada (`ValueError`).
- El riesgo teórico simultáneo de 2,5 % no se valida aparte: queda acotado por 5 posiciones × 0,5 % del equity en la entrada.

### Ejecución simulada y backtesting (Hito 3)

Costes (`CostModel`, perfil base): comisión 0,05 % y slippage 0,05 % por ejecución.

```text
compra: ejecutado = teórico * (1 + slippage)
venta:  ejecutado = teórico * (1 - slippage)
comisión = ejecutado * cantidad * commission_rate
```

Escudo anti-gap (`is_gap_filtered`):

- Cancela la entrada si `Open[t+1] - Close[t] > umbral * ATR[t]`; umbral por defecto `0.5`, configurable.
- Usa la apertura teórica (sin slippage) y el ATR del cierre de `t`.
- Un gap exactamente igual al umbral se permite. Los gaps bajistas nunca se filtran.
- Se registra como `GapFilteredEntry` (`GAP_FILTERED`) con el gap y el máximo permitido. No ocupa plaza ni participa en la priorización.

Reglas de salida Long (`evaluate_bar_exit`), con `SL = entrada_ejecutada - ATR` y `TP = entrada_ejecutada + 2 * ATR`:

1. Si `Low <= SL`, sale por `STOP_LOSS` aunque la barra también alcance el TP: el SL tiene prioridad absoluta.
2. El SL se ejecuta en `min(Open, SL)`: con gap bajista, a la apertura (peor que el nivel).
3. Si sólo `High >= TP`, sale por `TAKE_PROFIT` al nivel del TP, incluso con gap alcista (no se asume mejora de precio). Decisión confirmada como política conservadora de la fase inicial.

Salida temporal: si tras `max_holding_sessions = 20` sesiones (la de entrada cuenta como la primera) la posición sigue abierta, se cierra en la apertura de la sesión 21 (`sessions_since_entry = 20`). Si esa apertura ya está bajo el SL, se registra como `STOP_LOSS`.

Fin de datos: las posiciones abiertas se cierran al último cierre disponible con motivo `END_OF_DATA`, para no dejar PnL sin realizar.

`Trade` registra: señal, entrada y salida (timestamps), cantidad, precios teórico y ejecutado de entrada y salida, ATR de entrada, SL, TP, riesgo planificado, comisiones, coste de slippage, costes totales, PnL bruto, PnL neto, retorno porcentual y motivo de salida.

```text
gross_pnl   = (salida_teórica - entrada_teórica) * cantidad
total_costs = comisión_entrada + comisión_salida + slippage
net_pnl     = gross_pnl - total_costs
return_pct  = net_pnl / (entrada_ejecutada * cantidad + comisión_entrada)
```

`BacktestEngine.run(bars_by_symbol)`, en cada sesión:

1. Apertura: salidas temporales.
2. Apertura: entradas de las señales del cierre anterior del símbolo → anti-gap → `PortfolioAllocator` con el equity valorado al cierre anterior.
3. Barra: SL/TP de todas las posiciones abiertas, incluida la de la propia sesión de entrada.
4. Cierre: valoración a mercado y punto de la curva de equity (`EquityPoint`).

Las señales se precalculan por símbolo con `LongSignalGenerator`, cuya invariancia a barras futuras está cubierta por tests; la señal de la barra `k` sólo se usa en la barra `k+1`. `BacktestParameters` se devuelve con el resultado y exige que la comisión del sizing coincida con la del `CostModel`.

Limitaciones actuales:

- Opera sobre barras diarias: la granularidad intradía queda pendiente del proveedor. Con barras diarias, la regla de prioridad del SL se aplica siempre que la vela toque ambos niveles.
- Sin calendario, las sesiones se cuentan con las barras de cada símbolo; con calendario, la salida temporal cuenta sesiones del calendario.
- Una salida por SL/TP durante la barra no libera plaza para entradas de esa misma apertura.

### Ingesta de datos reales: Yahoo Finance

`YFinanceProvider` (`data/providers/yfinance.py`) implementa `MarketDataProvider` con `yfinance` para datos diarios (`source = "yfinance"`, `timeframe = "1d"`).

Yahoo entrega OHLCV y dividendos **ya ajustados por splits** (también con `auto_adjust=False`). Para que `market_bars_raw` sea realmente raw y `AdjustedMarketDataService` no ajuste dos veces, el adaptador deshace los splits:

```text
factor        = factor acumulado de splits posteriores a la barra (SplitAdjustmentCalculator)
precio_raw    = precio_yahoo / factor
volumen_raw   = volumen_yahoo * factor
dividendo_raw = dividendo_yahoo / factor
```

- Se usan todos los splits conocidos hasta hoy, no sólo los de la ventana: Yahoo ajusta el histórico por splits futuros a la ventana.
- `timestamp` diario = fecha de sesión de la bolsa a las 00:00 UTC.
- Valores redondeados a 8 decimales (escala de MySQL). Yahoo publica precios con precisión de coma flotante, por lo que los raw reconstruidos arrastran ruido del orden de 1e-7 relativo (p. ej. `499.23001099`). Es irrelevante para el backtest; la política de redondeo a tick sigue aplazada.
- Filas con valores ausentes se omiten; filas con OHLC inconsistente se descartan y se informan (`rejected_rows`).
- `snapshot_id = yfinance:1d:<símbolos>:<inicio>:<fin>`.
- `fetch_yahoo_history` es la única función que toca `yfinance`/`pandas` (sin tipos; override de mypy sólo para `yfinance`). El resto recibe DTOs tipados, y los tests usan un fetcher falso sin red.
- Timeout de 30 s por llamada a Yahoo. En la primera carga una descarga quedó bloqueada ~10 min antes de completarse; desde entonces las acciones corporativas se piden con `history(period="max")`, que sí acepta timeout.

CLI `algo-ingest` (`cli/ingest.py`, también `python -m algo_trading.cli.ingest`):

```powershell
algo-ingest                                   # AAPL MSFT GOOGL SPY QQQ desde 2020-01-01 hasta ayer
algo-ingest --symbols AAPL --start 2021-01-04 --end 2021-12-31
```

Por símbolo:

1. Reanuda desde el día siguiente a la última barra raw (o `--start`). El fin por defecto es ayer en UTC para no guardar la sesión en curso incompleta.
2. Si hay barras nuevas, guarda un snapshot con `MarketDataSnapshotIngestionService` (raw, splits y dividendos). Sin barras nuevas no crea snapshot.
3. Regenera las barras ajustadas del símbolo con `AdjustedMarketDataService.rebuild_for_symbol`: borra y reinserta la versión de ajuste (`splits-v1`) en una transacción, porque un split nuevo cambia todo el histórico ajustado.
4. Un fallo en un símbolo no detiene al resto; código de salida 1 si alguno falla.

Carga inicial (2026-10-22), 2020-01-02 → 2026-10-21:

```text
market_bars_raw       8550  (1710 por símbolo, todas con snapshot_id)
market_bars_adjusted  8550
corporate_actions      120  (2 splits: AAPL 4:1 2020-08-31, GOOGL 20:1 2022-07-18; 118 dividendos)
data_snapshots           5
```

Verificado: AAPL 2020-08-28 raw 499.23 → ajustado 124.81 (factor 0.25); GOOGL 2022-07-15 raw 2235.55 → ajustado 111.78 (factor 0.05). Los ajustados coinciden con los precios de Yahoo. Una segunda ejecución es idempotente.

### Backtest sobre datos de MySQL

`BacktestRunService` (`backtesting/run_service.py`) lee `market_bars_adjusted` (sólo lectura) por símbolo, `source` y `adjustment_version`, convierte las filas a `MarketBar` con timestamp UTC, ejecuta `BacktestEngine` y devuelve el `BacktestReport`.

- Periodo con fechas de sesión inclusivas: `[start 00:00, end 23:59:59]` sobre el repositorio inclusivo.
- Si algún símbolo no tiene barras en el periodo, falla con `MissingMarketDataError` indicando cuáles.

CLI `algo-backtest` (`cli/backtest.py`):

```powershell
algo-backtest                                  # universo inicial, 2020-01-01 → ayer
algo-backtest --symbols AAPL MSFT --start 2021-01-01 --end 2025-12-31 --gap-threshold-atr 1
```

- Capital inicial y máximo de posiciones por defecto desde `configs/environments/local.yaml`; costes del perfil base.
- Escribe `reports/<nombre>.json` y `reports/<nombre>.md`; nombre por defecto `backtest_<símbolos>_<inicio>_<fin>`. `reports/` está en `.gitignore`.
- El informe es determinista: dos ejecuciones con los mismos datos producen ficheros idénticos.

Primer backtest real (2026-10-22): AAPL, GOOGL, MSFT, QQQ, SPY; 2020-01-02 → 2026-10-21; capital 100.000; parámetros v1.

```text
                    Estrategia    Buy & Hold
Retorno acumulado      5.11 %      274.02 %
Maximum Drawdown       3.41 %       33.36 %
Trades 158 · Win Rate 41.77 % · Profit Factor 1.27 · Expectancy 0.10 R
Salidas: STOP_LOSS 91, TAKE_PROFIT 61, TIME_EXIT 6 · GAP_FILTERED 18 · rechazos 27
Tiempo en mercado 31.52 % · exposición media 4.86 % · costes 3,165.49
```

Lectura: la estrategia tiene expectativa positiva pero baja exposición (operaciones de ~5 sesiones con un 10 % máximo por posición), por lo que el capital pasa la mayor parte del tiempo en cash. Los costes consumen cerca del 38 % del beneficio bruto. Esta primera ejecución no incluía dividendos ni calendario (ver resultados actualizados abajo).

### Semántica temporal de rangos

Todos los rangos de lectura son **inclusivos**: `start <= x <= end`, igual que el proveedor y la ingesta.

- `RawMarketBarQueryRepository.get_by_symbol` y `AdjustedMarketBarQueryRepository.get_by_symbol`: `start <= timestamp <= end`.
- `CorporateActionQueryRepository` (splits y dividendos): `start <= ex_date <= end`.
- Para incluir una fecha de sesión completa con timestamps, `end` = esa fecha a las 23:59:59 (convención de `algo-ingest` y `BacktestRunService`).

### Calendario de mercado

`ExchangeCalendar` (`data/market_calendar.py`) usa `pandas-market-calendars` (por defecto `NYSE`; `NASDAQ` comparte festivos):

- `sessions(start, end)` devuelve `TradingSession` con apertura y cierre en UTC e `is_early_close`. Los festivos y cierres excepcionales (p. ej. 2025-01-09) no son sesiones; los cierres tempranos (p. ej. 2024-11-29, 18:00 UTC) se marcan.
- `check_session_coverage` compara las barras de un símbolo con las sesiones entre su primera y última barra: sesiones ausentes y barras en días sin sesión.
- `BacktestEngine.run(..., trading_sessions=...)`: si alguna serie no está completa lanza `SessionCoverageError` (con símbolos y fechas) y no se ejecuta ni se persiste nada. Con series completas, índice de barra = sesión de mercado (indicadores) y la salida temporal cuenta sesiones del calendario.
- Los cierres tempranos no afectan a la simulación diaria; quedan disponibles para la futura simulación intradía.
- Verificado con datos reales: NYSE tiene 1710 sesiones entre 2020-01-01 y 2026-10-21, exactamente las barras de cada símbolo ingerido.
- Es la única clase que toca `pandas-market-calendars` (sin tipos; override de mypy).

### Dividendos en cash

- `market_bars_raw` y `corporate_actions` guardan dividendos raw. Para operar con barras ajustadas por splits, `BacktestRunService` los lleva a la misma escala: `dividendo_ajustado = dividendo_raw * factor`, con el factor acumulado de los splits posteriores al ex_date.
- Estrategia (`BacktestEngine`, paso 0 de cada sesión): una posición abierta antes de la fecha ex-dividendo cobra `importe * cantidad` en cash en la primera sesión con fecha >= ex_date (normalmente la propia ex_date). Una entrada en la misma ex_date no cobra; una salida en la apertura de la ex_date sí cobra (se poseía al cierre anterior).
- Cada pago queda en `BacktestResult.dividends` (`DividendPayment`) y se suma a `Trade.dividend_income`; `net_pnl = gross_pnl - total_costs + dividend_income`. El PnL bruto no incluye dividendos.
- Buy & Hold: dividendos con ex_date posterior a la compra y hasta la última barra se acreditan en cash (no se reinvierten).
- `--no-dividends` en `algo-backtest` los desactiva para ambos.

### Persistencia de backtests

Migración `9c4d5e6f7a8b` (`alembic upgrade head`):

```text
backtest_runs              run_id (UUID), created_at, report_version, report_checksum,
                           símbolos, periodo, source, adjustment_version, trading_calendar,
                           parameters (JSON), métricas principales y Buy & Hold
backtest_trades            un registro por trade (precios, costes, dividendos, PnL, motivo)
backtest_equity_snapshots  curva de equity diaria, UNIQUE(run_id, timestamp)
```

- `BacktestRunService.run(request, persist=True)` guarda la ejecución, sus trades y su curva en una transacción y devuelve `BacktestRunOutcome(run_id, report, report_checksum)`. `report_checksum` es el SHA-256 del JSON del informe: dos ejecuciones con datos y parámetros idénticos tienen el mismo checksum.
- `BacktestRunRepository`: `add`, `get_summary`, `list_summaries` (más recientes primero), `get_trades`, `get_equity_curve`.
- Importes redondeados a 8 decimales y ratios a 10 al persistir (escala de las columnas).
- `algo-backtest --list-runs N` compara ejecuciones guardadas; `--no-persist` no escribe en MySQL.

Ejemplo de comparación SQL:

```sql
SELECT run_id, created_at, JSON_EXTRACT(parameters, '$.gap_threshold_atr') AS gap,
       cumulative_return, max_drawdown_pct, total_trades, profit_factor, buy_and_hold_return
FROM backtest_runs ORDER BY created_at DESC;
```

Resultados con calendario NYSE y dividendos (2026-10-22), 2020-01-02 → 2026-10-21:

```text
run (gap)     Retorno   MDD     Trades  PF    Dividendos  B&H       MDD B&H  Dividendos B&H
0.5 × ATR     5.27 %    3.41 %  158     1.28  151.02      282.93 %  32.47 %  8,906.61
1.0 × ATR     5.10 %    3.68 %  171     1.25  151.02      282.93 %  32.47 %  8,906.61
```

El calendario no cambió los trades (las series ya cubrían todas las sesiones). Relajar el anti-gap a 1.0 añade 13 operaciones y empeora ligeramente retorno, MDD y Profit Factor.

### Perfiles de costes y análisis de sensibilidad

Perfiles de costes:

- `ApplicationSettings.cost_profiles` (`CostProfileSettings`: `commission_rate`, `slippage_rate`) se lee de `local.yaml`; si falta la sección, se usan los tres perfiles por defecto (`default_cost_profiles`).
- `algo-backtest --cost-profile optimista|base|conservador` (sin distinguir mayúsculas) construye el `CostModel` y fija la misma comisión en el sizing. Un perfil desconocido falla listando los disponibles.
- El nombre del perfil queda en `BacktestParameters.cost_profile`, en el informe y en `backtest_runs.parameters`.

Resultados reales (parámetros v1, 2020-01-02 → 2026-10-21):

```text
Perfil        Retorno   MDD      Trades  Win Rate  PF
optimista     7.53 %    3.28 %   157     43.31 %   1.42
base          5.27 %    3.41 %   158     41.77 %   1.28
conservador   2.34 %    3.66 %   158     41.14 %   1.12
```

Multiplicador de stop loss:

- `RiskParameters.stop_loss_atr_multiple` (v1: 1) y `take_profit_atr_multiple` (v1: 2). `SL = entrada - k_SL * ATR`, `TP = entrada + k_TP * ATR`; ambos quedan en `ExitLevels`.
- El sizing usa el nuevo riesgo por unidad (`k_SL * ATR`): un stop más amplio reduce la cantidad por riesgo, de modo que el riesgo planificado sigue siendo como máximo el 0,5 % del equity.
- CLI: `--stop-loss-atr` y `--max-position-exposure` para ejecuciones individuales.

Barrido de parámetros (`backtesting/sensitivity.py`):

- `ParameterGrid`: producto cartesiano de `stop_loss_atr_multiples` (por defecto 1.0, 1.5, 2.0) y `max_position_exposures` (por defecto 10 %, 15 %, 20 %). El TP se mantiene en 2 × ATR.
- `SensitivityAnalysisService` lee barras, dividendos y sesiones una sola vez (`BacktestRunService.prepare`) y ejecuta un backtest por combinación (`run_prepared`). Con persistencia, todas las ejecuciones comparten `sweep_id` (columna nueva en `backtest_runs`, migración `a1d2e3f4b5c6`).
- `algo-backtest --sweep [--sl-multiples ...] [--exposures ...]` imprime la tabla y escribe `reports/sensitivity_<símbolos>_<inicio>_<fin>_<perfil>.json|.md`.
- `algo-backtest --list-runs N --sweep-id <id>` lista las ejecuciones de un barrido desde MySQL.

Barrido real (perfil base, 9 combinaciones, 10,6 s):

```text
SL×ATR  Exposición  Retorno   MDD      Win Rate  PF    Expectancy R  Trades
1.0     10 %         5.27 %   3.41 %   41.77 %   1.28  0.11          158
1.0     15 %         7.59 %   5.09 %   41.77 %   1.27  0.11          158
1.0     20 %         9.57 %   6.55 %   41.77 %   1.26  0.11          158
1.5     10 %         8.00 %   2.78 %   52.94 %   1.38  0.12          153
1.5     15 %        10.32 %   4.06 %   52.94 %   1.34  0.12          153
1.5     20 %        10.89 %   4.78 %   52.94 %   1.30  0.12          153
2.0     10 %        10.36 %   3.02 %   60.40 %   1.50  0.14          149
2.0     15 %        11.54 %   3.89 %   60.40 %   1.43  0.14          149
2.0     20 %        11.21 %   4.30 %   60.40 %   1.38  0.14          149
Buy & Hold: 282.93 %
```

Lectura: con TP fijo en 2 × ATR, ampliar el stop sube el Win Rate y el Profit Factor; ampliar la exposición sube el retorno y el MDD casi en proporción, y a partir del 15 % la falta de cash limita las entradas. Es una optimización dentro de la muestra (un solo periodo y cinco activos muy correlacionados): antes de cambiar los parámetros v1 conviene validarla fuera de muestra (walk-forward).

### Walk-Forward Optimization

`analytics/walk_forward.py` mide la solidez fuera de muestra de la rejilla de parámetros (SL × ATR y exposición):

- `build_windows`: ventanas rodantes IS de `in_sample_months` (por defecto 36) seguidas de OOS de `out_of_sample_months` (por defecto 12); paso = OOS por defecto, de modo que las OOS son contiguas y no se solapan (un paso menor se rechaza). La última OOS se recorta al fin de los datos y se marca como parcial.
- En cada IS se ejecuta la rejilla y `select_best` elige la combinación con mayor Profit Factor (o Sharpe con `--wf-metric sharpe`). Sólo son elegibles las combinaciones con al menos `min_trades` trades (por defecto 10); Profit Factor sin pérdidas cuenta como infinito; los empates mantienen el orden de la rejilla.
- La combinación elegida se aplica sin cambios a la OOS siguiente. Las OOS se encadenan: cada una empieza con el equity final de la anterior y forman la curva OOS.
- Sin sesgo: cada ejecución recibe sólo barras hasta el fin de su ventana; las anteriores sólo calientan indicadores mediante `BacktestEngine.run(..., trading_start=...)`, que bloquea entradas y equity antes del inicio. Se usa siempre el historial desde el inicio de los datos, así los indicadores (incluido el ATR de Wilder) son idénticos en todas las ventanas. Las posiciones abiertas al final de una ventana se cierran a su último cierre.
- Métricas por periodo: retorno acumulado y anualizado (`(1+R)^(365.25/días) - 1`), MDD, Profit Factor, Sharpe anualizado (retornos diarios, tasa libre 0, `sqrt(252)`), Win Rate y trades.
- WFE = retorno anualizado OOS / retorno anualizado IS. Por ventana usa la combinación elegida; el global usa la curva OOS encadenada y la media de los IS elegidos. No calculable si el IS anualizado no es positivo. WFE > 50 % se considera robusto.
- Los datos se leen una vez de MySQL (`BacktestRunService.prepare`); el análisis corre en memoria y no persiste ejecuciones.
- Tests: un fallo inyectado que deja ver datos posteriores a la ventana hace fallar los tests de no look-ahead.

CLI:

```powershell
algo-backtest --walk-forward                                   # IS 36 / OOS 12, Profit Factor
algo-backtest --walk-forward --is-months 24 --oos-months 6 --wf-metric sharpe --min-trades 5
```

Escribe `reports/walk_forward_<símbolos>_<inicio>_<fin>_<perfil>_is<IS>-oos<OOS>_<métrica>.md|.json` con el resumen OOS, una fila por ventana, la rejilla IS completa de cada ventana y notas de método.

Resultado real (2026-10-23; perfil base; IS 36 / OOS 12; 40 s):

```text
#  OOS                        Elegida (SL, exp.)  Anual. IS  Anual. OOS  PF OOS  Trades  WFE
1  2023-01-01 → 2023-12-31    2.0, 10 %           2.19 %      0.98 %     1.28    25       44.96 %
2  2024-01-01 → 2024-12-31    2.0, 10 %           1.72 %     -0.74 %     0.81    24      -42.96 %
3  2025-01-01 → 2025-12-31    1.5, 10 %           0.84 %      1.18 %     1.41    23      140.21 %
4  2026-01-01 → 2026-10-21    1.0, 20 %           1.66 %      2.76 %     1.76    13      165.76 %

OOS encadenado: retorno 3.66 %, anualizado 0.95 %, MDD 2.96 %, PF 1.28, Sharpe 0.43, 85 trades
IS anualizado medio 1.60 % → WFE 59.14 % (selección por Sharpe: OOS 4.08 %, WFE 59.31 %)
```

Lectura: el WFE supera el 50 %, pero se calcula sobre retornos anualizados muy pequeños (≈1-2 %), por lo que es sensible a pocas operaciones; la ventana 2024 fue negativa y los parámetros elegidos cambian entre ventanas. La estrategia conserva fuera de muestra parte de su ventaja, pero con un retorno absoluto muy inferior a Buy & Hold.

### Universo diversificado y walk-forward ampliado (Ruta 1)

Universos (`configs/environments/local.yaml`, `ApplicationSettings.universes`):

```text
initial      AAPL MSFT GOOGL SPY QQQ
diversified  AAPL MSFT GOOGL QQQ SPY XOM JPM JNJ PG CAT GLD TLT XLE XLF XLV
```

- `algo-ingest --universe diversified` y `algo-backtest --universe diversified` (excluyente con `--symbols`; sin ninguno, `initial`). Con `--universe`, el nombre del universo sustituye a la lista de símbolos en el nombre de los informes.
- Ingesta (2026-10-23, 112 s): 15 símbolos × 1711 barras (2020-01-02 → 2026-10-22, todas las sesiones NYSE), 25.665 barras raw y ajustadas, 419 acciones corporativas (3 splits: AAPL 4:1, GOOGL 20:1, XLE 2:1 el 2025-12-05) y 20 snapshots. XLE 2025-12-04: raw 92.22 → ajustado 46.11.

Rejilla ampliada:

- `ParameterGrid` tiene tres ejes: `stop_loss_atr_multiples`, `take_profit_atr_multiples` y `max_position_exposures`; `combinations()` devuelve `ParameterCombination` (orden SL → TP → exposición) con `apply(parameters)`.
- El TP por defecto sigue en 2 × ATR (rejilla 3 × 3); `--tp-multiples 2.0 2.5 3.0` da la rejilla 3 × 3 × 3 (27 combinaciones). `--take-profit-atr` para ejecuciones individuales.
- El walk-forward añade por periodo `time_in_market` y `average_exposure`, Buy & Hold equiponderado del universo por ventana OOS y para todo el periodo OOS (mismos costes, dividendos en cash), y una sección de uso de capital en el informe (`walk-forward-report-v2`).

Ejecución (IS 36 / OOS 12, Profit Factor, perfil base, 27 combinaciones):

```powershell
algo-backtest --walk-forward --universe diversified --tp-multiples 2.0 2.5 3.0
```

Informe: `reports/walk_forward_diversified_2020-01-01_2026-10-22_base_is36-oos12_profit_factor.md` (201 s, 112 backtests).

```text
#  OOS                       Elegida (SL, TP, exp.)  Anual. IS  Anual. OOS  B&H OOS   PF OOS  Trades  WFE
1  2023                      2.0, 3.0, 10 %          1.95 %     0.98 %      19.95 %   1.12    47       50.46 %
2  2024                      2.0, 3.0, 15 %          2.32 %     5.29 %      18.95 %   1.55    53      227.76 %
3  2025                      1.0, 3.0, 20 %          4.01 %     4.88 %      24.38 %   1.38    58      121.65 %
4  2026-01-01 → 2026-10-22   1.0, 3.0, 20 %          4.80 %     0.16 %      17.06 %   1.01    50        3.28 %
```

Comparación con el universo inicial (misma rejilla de 27 combinaciones y periodo):

```text
                                 diversified (15)   initial (5)
WFE global                       89.82 %            59.10 %
Retorno acumulado OOS            11.66 %             3.66 %
Retorno anualizado OOS            2.94 %             0.95 %
MDD OOS                           4.12 %             2.96 %
Profit Factor / Sharpe OOS       1.24 / 0.62        1.28 / 0.43
Trades OOS                       208                85
Tiempo en mercado                82.51 %            42.09 %
Exposición media                 27.93 %             7.02 %
Buy & Hold OOS (equiponderado)  108.77 %           164.57 %
Exceso frente a Buy & Hold      -97.12 %          -160.91 %
TP elegido                       3.0 en todas       2.0 en todas
```

Lectura:

- Diversificar mejora claramente la estrategia: más señales independientes (208 trades frente a 85), el capital trabaja cuatro veces más (exposición media 28 % frente a 7 %) y el retorno OOS se triplica con un MDD todavía bajo (4,1 %). El WFE pasa de 59 % a 90 % y la combinación elegida es estable en TP (3 × ATR en todas las ventanas).
- La ventana 2026 (parcial) casi no gana (WFE 3 %): el WFE global depende de pocas ventanas y conviene seguirlo con más historia.
- Aun así, Buy & Hold diversificado (108,8 %) supera ampliamente a la estrategia (11,7 %) en este periodo alcista, con un MDD mucho mayor (15,3 %). El 72 % del capital sigue ocioso: el límite de 5 posiciones y el riesgo de 0,5 % por operación son ahora el principal freno al retorno absoluto.

### Escalado de cartera y riesgo en walk-forward

Rejilla:

- `ParameterCombination` y `ParameterGrid` añaden `max_positions` y `risk_per_trade` (por defecto 5 y 0,5 %: la rejilla anterior no cambia). Orden: SL → TP → exposición → posiciones → riesgo. `scale_levels()` devuelve los pares (posiciones, riesgo).
- CLI: `--positions-grid`, `--risk-grid` (ejes de la rejilla), `--max-positions`, `--risk-per-trade` (ejecuciones individuales) y `--report-tag` (etiqueta en el nombre del informe). `build_grid` construye la rejilla para barrido y walk-forward.

Walk-forward:

- La rejilla IS se evalúa una sola vez por ventana. El walk-forward principal elige entre todas las combinaciones; el estudio de escalado (`PortfolioScaleResult`, informe `walk-forward-report-v3`) repite sólo la selección restringida a cada nivel (posiciones, riesgo) y encadena su OOS. Un test comprueba que cada nivel coincide con un walk-forward ejecutado sólo con ese nivel.
- Rendimiento: las señales se calculan una vez sobre todo el historial (`WalkForwardAnalyzer.signals`) y el motor las recibe con `precomputed_signals`; son invariantes a barras futuras, así que el resultado es idéntico (test de equivalencia y walk-forward diversificado anterior reproducido exactamente: 11,66 %, WFE 89,82 %) y unas 10 veces más rápido (201 s → 19 s).
- Métrica nueva por periodo: `average_open_positions`.

Ejecución:

```powershell
algo-backtest --walk-forward --universe diversified --sl-multiples 1.5 2.0 --tp-multiples 2.5 3.0 `
  --exposures 0.10 0.15 0.20 --positions-grid 5 8 10 --risk-grid 0.005 0.010 0.015 --report-tag portfolio_scaling
```

Informe: `reports/walk_forward_portfolio_scaling_diversified_2020-01-01_2026-10-22_base_is36-oos12_profit_factor.md` (108 combinaciones por ventana, 9 niveles, 89 s).

```text
Pos.  Riesgo  Riesgo máx.  Retorno OOS  MDD     Sharpe  Exposición  Pos. medias  Trades  WFE
5     0,5 %    2,5 %       11.01 %      4.23 %  0.65    27.60 %     2.01         202     119.56 %
5     1,0 %    5,0 %       14.05 %      5.49 %  0.64    33.46 %     2.06         198     131.63 %
5     1,5 %    7,5 %        6.92 %      7.28 %  0.39    25.37 %     2.01         202      80.09 %
8     0,5 %    4,0 %        7.96 %      5.12 %  0.47    27.64 %     2.08         218      90.60 %
8     1,0 %    8,0 %        5.45 %      7.14 %  0.30    28.09 %     2.07         217      67.12 %
8     1,5 %   12,0 %        5.30 %      7.28 %  0.29    28.04 %     2.05         216      61.94 %
10    (idéntico a 8 posiciones en los tres niveles de riesgo)
Optimizado (108 comb.)      9.48 %      5.49 %  0.52    29.49 %     2.02         204      92.65 %
Buy & Hold diversificado OOS: 108.77 % (MDD 15.27 %)
```

Diagnóstico del capital ocioso:

- No faltan plazas: la media es de 2 posiciones abiertas y sólo el 5,8 % de las sesiones OOS llega a 5. Con 8 o 10 plazas apenas entran operaciones nuevas (≈+15 trades) y son de peor calidad; 8 y 10 dan resultados idénticos.
- El riesgo por operación casi no cambia el tamaño: la mediana de ATR/precio en la entrada es 1,58 % (p90 2,41 %). Con SL de 1,5-2 × ATR, el riesgo por unidad es ≈2,4-3,2 % del precio, de modo que 0,5 % de riesgo ya pide ≈20 % del equity por posición y el tope de exposición (10-20 %) es el que limita. Subir el riesgo sólo amplía las posiciones de ATR alto y empeora el MDD (7,3 % con 1,5 %).
- Optimizar también la escala (108 combinaciones) da menos que fijar 5 posiciones (9,48 % frente a 11-14 %): más grados de libertad añaden ruido de selección aunque el WFE siga alto.
- Conclusión: el cuello de botella es la frecuencia de señales × el tope de exposición por posición, no el número de plazas ni el riesgo. Mantener 5 posiciones y 0,5-1,0 % de riesgo (con 1,0 % se supera el límite teórico de 2,5 % de la especificación v1: 5 %). Las palancas útiles son la exposición máxima por posición y más señales.

### Cierre de la v1: valores por defecto bloqueados

- `ApplicationSettings.strategy` (`StrategySettings`: `risk_per_trade`, `stop_loss_atr_multiple`, `take_profit_atr_multiple`, `max_position_exposure`) y `default_universe` (validado contra `universes`) se leen de `local.yaml`.
- `algo-backtest`: `--risk-per-trade`, `--stop-loss-atr`, `--take-profit-atr` y `--max-position-exposure` valen `None` por defecto y toman el valor de la configuración; `--max-positions` toma `max_positions`. Sin `--symbols` ni `--universe`, ambas CLIs usan `default_universe`, que también da nombre a los informes.
- Las rejillas de barrido y walk-forward mantienen sus valores por defecto explícitos (`--sl-multiples`, `--tp-multiples`, `--exposures`); las de posiciones y riesgo, sin `--positions-grid` / `--risk-grid`, usan los valores base de la configuración.

### Métricas e informes (Hito 4)

`calculate_performance_metrics(result, bars_by_symbol)` (`analytics/metrics.py`):

- **Retorno acumulado**: `cash_final / capital_inicial - 1`. El equity final es el cash tras cerrar todas las posiciones, incluidas las `END_OF_DATA`.
- **Curva de equity**: la del motor (`EquityPoint` al cierre de cada sesión), incluida en el informe.
- **Trades** (PnL neto; ganador si `net_pnl > 0`, el empate cuenta como perdedor):
  - Win Rate = ganadores / trades.
  - Profit Factor = beneficio bruto de ganadores / |pérdidas|; `None` si no hay pérdidas.
  - Expectancy = PnL neto medio por trade; Expectancy en R = media de `net_pnl / riesgo_planificado`.
  - Ganancia media, pérdida media, costes totales y salidas por motivo.
  - Duración media en sesiones (`sessions_since_entry`) y en días naturales.
- **Maximum Drawdown**: máxima caída porcentual desde un máximo previo, con el capital inicial como primer máximo; incluye importe y timestamps de pico y valle.
- **Exposición**: tiempo en mercado (sesiones con posiciones / total) y exposición media (`valor_mercado / equity`).
- **Buy & Hold**: equiponderado sobre los mismos símbolos; compra en la apertura de la primera barra de cada símbolo y venta al último cierre, con los mismos costes y unidades fraccionarias. Incluye retorno, MDD y exceso de retorno de la estrategia.
- Las ratios sin denominador válido son `None` (`n/d` en Markdown).

`build_report` → `BacktestReport` (`analytics/report.py`):

- Contiene versión del informe, símbolos, periodo, parámetros completos, métricas, trades, entradas `GAP_FILTERED`, rechazos y curva de equity.
- `report_to_json`: JSON con los `Decimal` como texto (sin pérdida de precisión); se puede reconstruir con `BacktestReport.model_validate_json`.
- `report_to_markdown`: resumen legible con tablas de parámetros, resultados frente a Buy & Hold, estadísticas de trades y registro de operaciones.
- `write_report(report, directorio, nombre)`: escribe `<nombre>.json` y `<nombre>.md`.
- El informe no incluye la fecha de generación, por lo que es determinista: el mismo backtest produce exactamente el mismo informe.

---

## 8. Archivos importantes

### Configuración

```text
pyproject.toml
.env.example
configs/environments/local.yaml
```

### Persistencia

```text
src/algo_trading/persistence/database.py
src/algo_trading/persistence/models/base.py
src/algo_trading/persistence/models/market_data.py
src/algo_trading/persistence/models/corporate_actions.py
src/algo_trading/persistence/models/adjusted_market_data.py
src/algo_trading/persistence/models/data_snapshot.py
```

### Migraciones

```text
alembic.ini
migrations/env.py
migrations/versions/
```

### Datos y ajustes

```text
src/algo_trading/data/market_bar.py
src/algo_trading/data/ingestion_service.py
src/algo_trading/data/validation/market_bar_validator.py
src/algo_trading/data/adjustments/split_adjustment.py
src/algo_trading/data/adjustments/adjusted_bar_builder.py
src/algo_trading/data/adjustments/adjusted_market_data_service.py
src/algo_trading/data/market_data_snapshot_persistence_service.py
src/algo_trading/data/market_data_snapshot_ingestion_service.py
src/algo_trading/data/providers/dto.py
src/algo_trading/data/providers/protocol.py
src/algo_trading/data/providers/in_memory.py
src/algo_trading/data/providers/yfinance.py
src/algo_trading/cli/ingest.py
src/algo_trading/backtesting/run_service.py
src/algo_trading/cli/backtest.py
src/algo_trading/data/market_calendar.py
src/algo_trading/persistence/models/backtest.py
src/algo_trading/persistence/repositories/backtest_run_repository.py
migrations/versions/9c4d5e6f7a8b_create_backtest_results.py
migrations/versions/a1d2e3f4b5c6_add_sweep_id_to_backtest_runs.py
src/algo_trading/backtesting/sensitivity.py
src/algo_trading/analytics/walk_forward.py
```

### Dominio

```text
src/algo_trading/domain/corporate_actions/dto.py
src/algo_trading/domain/corporate_actions/mapper.py
```

### Indicadores y estrategia

```text
src/algo_trading/indicators/price_series.py
src/algo_trading/indicators/moving_average.py
src/algo_trading/indicators/bollinger.py
src/algo_trading/indicators/atr.py
src/algo_trading/indicators/indicator_frame.py
src/algo_trading/strategy/long_signal.py
```

### Gestión monetaria

```text
src/algo_trading/portfolio/position_sizing.py
src/algo_trading/portfolio/exit_levels.py
src/algo_trading/portfolio/portfolio_state.py
src/algo_trading/portfolio/allocator.py
```

### Ejecución y backtesting

```text
src/algo_trading/execution/costs.py
src/algo_trading/execution/gap_filter.py
src/algo_trading/execution/exit_rules.py
src/algo_trading/execution/trade.py
src/algo_trading/backtesting/engine.py
```

### Métricas e informes

```text
src/algo_trading/analytics/metrics.py
src/algo_trading/analytics/report.py
```

### Repositorios

```text
src/algo_trading/persistence/repositories/market_bar_repository.py
src/algo_trading/persistence/repositories/market_bar_query_repository.py
src/algo_trading/persistence/repositories/adjusted_bar_repository.py
src/algo_trading/persistence/repositories/adjusted_bar_query_repository.py
src/algo_trading/persistence/repositories/corporate_action_query_repository.py
src/algo_trading/persistence/repositories/corporate_action_repository.py
src/algo_trading/persistence/repositories/data_snapshot_repository.py
```

---

## 9. Estado actual de Git

Antes de cerrar este chat, ejecutar:

```powershell
git status
```

Debe mostrar:

```text
On branch main
nothing to commit, working tree clean
```

Comprobar el último commit:

```powershell
git log --oneline --max-count=5
```

Comprobar el remoto:

```powershell
git remote -v
```

Comprobar que no se subieron secretos:

```powershell
git ls-files | Select-String "\.env$|\.venv"
```

Este último comando no debe devolver resultados.

---

## 10. Validación de cierre

Con Docker Desktop activo y MySQL iniciado:

```powershell
docker compose ps
```

El contenedor debe estar:

```text
healthy
```

Validar base de datos:

```powershell
alembic current
```

Validar tablas:

```powershell
docker compose exec mysql mysql `
    -u algo_trading_app `
    -palgo-trading-dev-password `
    algo_trading `
    -e "SHOW TABLES;"
```

Resultado esperado:

```text
alembic_version
corporate_actions
market_bars_adjusted
market_bars_raw
```

Validar código:

```powershell
pytest
ruff check .
ruff format --check .
mypy src
```

---

## 11. Problemas o decisiones pendientes

### 11.1 Fechas y zonas horarias

Actualmente MySQL utiliza columnas `DateTime` sin zona horaria y el código guarda UTC sin información de timezone:

```python
datetime.now(UTC).replace(tzinfo=None)
```

Esto debe documentarse y mantenerse consistente en todo el sistema.

### 11.2 Corporate actions

Implementado: acreditación de dividendos en cash en la fecha ex-dividendo (estrategia y Buy & Hold).

Falta implementar:

- Validación cruzada entre tipo de acción y campo requerido.
- Versionado formal de snapshots de acciones corporativas.

### 11.3 Proveedor de datos

Implementado:

- `MarketDataProvider` como contrato estable.
- DTOs de solicitudes y snapshots.
- Proveedor `InMemoryMarketDataProvider` para pruebas.
- Ingesta, validación, persistencia atómica y trazabilidad de snapshots.
- Checksum reproducible e idempotencia por `snapshot_id` (ver sección 7).
- `YFinanceProvider` (Yahoo Finance, diario) y CLI `algo-ingest` (ver sección 7).

Pendiente:

- Reintentos con backoff y control de rate limits: hoy hay timeout de 30 s por llamada, sin reintentos; el fallo queda aislado por símbolo y una nueva ejecución reanuda.
- Proveedor intradía.

### 11.4 Datos intradía

Pendiente decidir:

- Proveedor definitivo.
- Granularidad: 1 minuto, 5 minutos u otra.
- Retención disponible.
- Política de ajuste respecto a datos diarios.
- Tratamiento de sesiones regulares y cierres tempranos.

### 11.5 Calendario

Integrado `pandas-market-calendars` (ver «Calendario de mercado»): bolsa NYSE por defecto, horarios en UTC, festivos, cierres excepcionales y tempranos, y conteo exacto de sesiones para indicadores y salida temporal.

Pendiente: uso de horarios y cierres tempranos en la simulación intradía.

### 11.6 Precisión

Decisión actual:

- `Decimal` para dinero, precios persistidos, cantidades y PnL.
- `float64` potencial para cálculos vectorizados de indicadores, si se justifica.

Debe definirse la frontera exacta entre Pandas/NumPy y el dominio financiero.

### 11.7 Modelos pendientes

Implementados como DTOs: `LongSignal`, `Position`, `Trade`, `EquityPoint`, `DividendPayment`. Persistidos: `BacktestRun`, `Trade` y `EquitySnapshot` (`backtest_runs`, `backtest_trades`, `backtest_equity_snapshots`).

Pendientes (necesarios para simulación intradía o ejecución real):

```text
Order
Fill
```

---

## 12. Próximo orden de implementación

El contrato de proveedores, los snapshots, la persistencia atómica y la trazabilidad ya están implementados.

Completado:

- Algoritmo reproducible de `checksum` para `DataSnapshot`.
- Convención de `snapshot_id` y política de idempotencia.
- DTO de lectura para dividendos en `CorporateActionQueryRepository`.
- Hito 1: indicadores (SMA, Bollinger, ATR Wilder) y generación de señales Long.
- Hito 2: position sizing, niveles SL/TP, estado de portfolio y priorización de señales simultáneas.
- Hito 3: costes, escudo anti-gap, reglas de salida con prioridad del SL, salida temporal, registro de trades y motor de backtesting diario.
- Hito 4: métricas de performance, comparación Buy & Hold y exportación del informe en JSON/Markdown.
- Ingesta real: `YFinanceProvider`, CLI `algo-ingest` y carga inicial de AAPL, MSFT, GOOGL, SPY y QQQ (2020 → 2026-10-21).
- Backtest sobre MySQL: `BacktestRunService` y CLI `algo-backtest` con informe JSON/Markdown.
- Rangos de lectura inclusivos en todos los repositorios.
- Calendario de mercado NYSE con validación de sesiones y conteo exacto de la salida temporal.
- Dividendos acreditados en cash (estrategia y Buy & Hold).
- Persistencia y comparación de ejecuciones (`backtest_runs`, `backtest_trades`, `backtest_equity_snapshots`).
- Perfiles de costes optimista, base y conservador seleccionables desde la CLI.
- Multiplicador de stop loss configurable y barrido de parámetros (SL × exposición) persistido con `sweep_id`.
- Walk-Forward Optimization con curva OOS encadenada y Eficiencia Walk-Forward (`--walk-forward`).
- Universo diversificado (15 símbolos) ingerido; rejilla con eje de TP; walk-forward con uso de capital y Buy & Hold diversificado.
- Escalado de cartera (posiciones y riesgo) en la rejilla y estudio de escalado por nivel en el walk-forward; señales precalculadas (≈10× más rápido).
- **Cierre de la v1**: valores por defecto bloqueados (diversified, 5 posiciones, 1 % de riesgo, SL 2×, TP 3×, exposición 20 %) y backtest de confirmación guardado.

- **Inicio de la v2**: especificación técnica `docs/technical-spec-v2.md` con arquitectura, requisitos y roadmap de cinco hitos.

Orden inmediato (Hito 1 de la v2, ver `docs/technical-spec-v2.md`):

1. Test de regresión que fije el informe de la v1 con las funciones v2 desactivadas.
2. Contrato `Signal` / `SignalGenerator` y adaptador de `LongSignalGenerator` (`strategy_id`, `score`).
3. Indicadores nuevos (volumen relativo, fuerza relativa, RSI, z-score, percentil, correlación) con tests de no look-ahead.
4. Ingesta de benchmarks, ETFs sectoriales y `^VIX` como serie de referencia.
5. `CompositeSignalGenerator` con filtros, Short y reversión a la media en el motor; validación con walk-forward frente al baseline v1.

Decisiones v2 propuestas que cierran pendientes de la v1: proveedor intradía Alpaca Market Data (1m / 5m, feed IEX), reintentos con backoff en el pipeline en vivo y modelos `Order` / `Fill`. No conectar el broker a la estrategia antes del Hito 3, y nunca a una cuenta real.

---

## 13. Próximo chat

Al iniciar un chat nuevo, proporcionar:

```text
Proyecto: algo-trading
Repositorio: GitHub, rama main
Python: 3.13.3
Sistema operativo: Windows 11
IDE: VS Code
Base de datos: MySQL 8.0 mediante Docker Compose
Estado: v1 cerrada (2026-10-23) con valores por defecto bloqueados: universo diversified, 5 posiciones, 1 % de riesgo, SL 2 × ATR, TP 3 × ATR, exposición 20 %; v2 en diseño
Validaciones: pytest, ruff y mypy están en verde
Siguiente tarea: Hito 1 de la v2 (motor de señales multi-factor)
Fuente de verdad: docs/project-status.md y docs/technical-spec-v2.md
```

Antes de continuar, pedir que se confirme la salida de:

```powershell
git status
pytest
ruff check .
ruff format --check .
mypy src
```

No regenerar módulos ya implementados. La v1 está cerrada: no cambiar sus valores por defecto sin una nueva validación walk-forward.

Al abrir el siguiente chat, bastará con indicar:

> Este proyecto continúa desde `docs/project-status.md`. Lee ese estado y `docs/technical-spec-v2.md`, y no regeneres módulos ya implementados. La v1 está cerrada con valores por defecto bloqueados; el siguiente paso es el Hito 1 de la v2.
