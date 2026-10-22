# Estado del proyecto — Algo Trading

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

Se conservarán perfiles:

- Optimista.
- Base.
- Conservador.

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

Limitación: sin calendario integrado, no se detectan sesiones faltantes; los índices `t-10` y `t-20` cuentan barras disponibles, no sesiones de mercado.

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
- Las sesiones se cuentan con las barras de cada símbolo, sin calendario de mercado.
- Una salida por SL/TP durante la barra no libera plaza para entradas de esa misma apertura.

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

Falta implementar:

- Validación cruzada entre tipo de acción y campo requerido.
- Servicio completo para acreditar dividendos a cash.
- Versionado formal de snapshots de acciones corporativas.

### 11.3 Proveedor de datos

Implementado:

- `MarketDataProvider` como contrato estable.
- DTOs de solicitudes y snapshots.
- Proveedor `InMemoryMarketDataProvider` para pruebas.
- Ingesta, validación, persistencia atómica y trazabilidad de snapshots.
- Checksum reproducible e idempotencia por `snapshot_id` (ver sección 7).

Pendiente:

- Implementar un proveedor externo real detrás del contrato.
- Decidir la política de reintentos, rate limits y errores transitorios del proveedor real.

### 11.4 Datos intradía

Pendiente decidir:

- Proveedor definitivo.
- Granularidad: 1 minuto, 5 minutos u otra.
- Retención disponible.
- Política de ajuste respecto a datos diarios.
- Tratamiento de sesiones regulares y cierres tempranos.

### 11.5 Calendario

Debe integrarse:

```text
pandas-market-calendars
```

Hay que definir:

- Bolsa objetivo.
- Zona horaria.
- Sesiones regulares.
- Festivos.
- Cierres tempranos.
- Conteo exacto del time exit.

### 11.6 Precisión

Decisión actual:

- `Decimal` para dinero, precios persistidos, cantidades y PnL.
- `float64` potencial para cálculos vectorizados de indicadores, si se justifica.

Debe definirse la frontera exacta entre Pandas/NumPy y el dominio financiero.

### 11.7 Modelos pendientes

Faltan entidades y DTOs para:

```text
Signal
Order
Fill
Position
Trade
BacktestRun
EquitySnapshot
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

Orden inmediato:

1. Revisar y unificar la semántica temporal de rangos: el proveedor y la ingesta usan límites inclusivos (`start <= x <= end`), mientras que los repositorios de lectura usan rangos semiabiertos (`start <= x < end`).
2. Integrar `pandas-market-calendars` para validar que las series no tengan huecos de sesiones antes de calcular indicadores y contar sesiones.
3. Servicio que cargue barras ajustadas desde MySQL, ejecute el backtest y guarde el informe (y, si se requiere, persistir `BacktestRun`, `Trade` y `EquitySnapshot`).
4. Diseñar el adaptador del proveedor externo real sin conectarlo todavía a la estrategia.
5. Decidir proveedor y granularidad intradía antes de implementar su adaptador y la simulación intradía.
6. Perfiles de costes optimista y conservador, y análisis de sensibilidad del umbral anti-gap.

No conectar todavía un proveedor externo directamente a la estrategia.

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
Estado: v1 completa en datos diarios: persistencia, snapshots, indicadores, señales Long, gestión monetaria, backtesting, métricas e informes
Validaciones: pytest, ruff y mypy están en verde
Siguiente tarea: unificar la semántica de rangos e integrar el calendario de mercado
Fuente de verdad: docs/project-status.md
```

Antes de continuar, pedir que se confirme la salida de:

```powershell
git status
pytest
ruff check .
ruff format --check .
mypy src
```

No regenerar módulos ya implementados. Continuar desde la semántica temporal de rangos y el adaptador del proveedor externo.

Al abrir el siguiente chat, bastará con indicar:

> Este proyecto continúa desde `docs/project-status.md`. Lee ese estado y no regeneres módulos ya implementados. El siguiente paso es unificar la semántica temporal de rangos entre proveedor, ingesta y repositorios de lectura.
