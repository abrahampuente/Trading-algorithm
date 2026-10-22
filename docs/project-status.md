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
├── execution/        # Pendiente
├── portfolio/        # Pendiente
├── backtesting/      # Pendiente
└── analytics/        # Pendiente
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
- `rolling_population_std` y `bollinger_bands`: desviación estándar poblacional (`ddof=0`), bandas `SMA ± k·σ` con `k = 2` por defecto (configurable).
- `true_ranges`: `max(H-L, |H-C[t-1]|, |L-C[t-1]|)`; la primera barra no tiene TR.
- `wilder_atr`: semilla `media(TR[1..20])` en el índice 20 y luego `ATR[t] = (ATR[t-1]·19 + TR[t]) / 20`. El primer ATR está disponible con 21 barras, coherente con el warm-up mínimo.
- `calculate_indicators` devuelve una tupla de `IndicatorRow` (equivalente a un DataFrame) con `sma`, `upper_band`, `lower_band` y `atr`.
- `LongSignalGenerator` emite `LongSignal` al cierre de `t` cuando `Close[t] > Close[t-10] > Close[t-20]`, `Close[t-1] <= UpperBand[t-1]` y `Close[t] > UpperBand[t]`. La señal incluye el ATR de `t` (para el filtro anti-gap y el SL/TP) y `momentum = Close[t] / Close[t-20] - 1` para priorizar señales simultáneas.

Garantía de no look-ahead:

- Cada indicador en `t` sólo lee barras `<= t`.
- `LongSignalGenerator.evaluate` recibe únicamente el historial hasta `t`; no tiene acceso a `t+1`.
- La señal no contiene precio de entrada: la entrada se simulará en la apertura de `t+1` en el motor de ejecución.
- Tests: los indicadores y señales calculados sobre cualquier prefijo coinciden con los de la serie completa, y alterar barras posteriores a una señal no la modifica. Ambos tests detectan un sesgo inyectado artificialmente.

Limitación: sin calendario integrado, no se detectan sesiones faltantes; los índices `t-10` y `t-20` cuentan barras disponibles, no sesiones de mercado.

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

Orden inmediato:

1. Revisar y unificar la semántica temporal de rangos: el proveedor y la ingesta usan límites inclusivos (`start <= x <= end`), mientras que los repositorios de lectura usan rangos semiabiertos (`start <= x < end`).
2. Confirmar el multiplicador de Bollinger (asumido `2`) y la métrica de fuerza del momentum (asumida `Close[t] / Close[t-20] - 1`).
3. Integrar `pandas-market-calendars` para validar que las series no tengan huecos de sesiones antes de calcular indicadores.
4. Diseñar el adaptador del proveedor externo real sin conectarlo todavía a la estrategia.
5. Decidir proveedor y granularidad intradía antes de implementar su adaptador.

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
Estado: infraestructura, persistencia, snapshots, trazabilidad, indicadores y señales Long implementados
Validaciones: pytest, ruff y mypy están en verde
Siguiente tarea: unificar la semántica temporal de rangos y preparar el motor de gestión monetaria
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
