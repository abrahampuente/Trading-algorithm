from decimal import Decimal


def is_gap_filtered(
    entry_open: Decimal,
    signal_close: Decimal,
    signal_atr: Decimal,
    threshold_atr: Decimal,
) -> bool:
    """
    Escudo anti-gap para entradas Long.

    Cancela la entrada si `Open[t+1] - Close[t] > threshold_atr * ATR[t]`.
    Sólo filtra gaps alcistas; un gap igual al umbral no se filtra. Usa el
    precio de apertura teórico (sin slippage) y el ATR del cierre de `t`.
    """
    if threshold_atr < 0:
        raise ValueError("threshold_atr no puede ser negativo")

    if signal_atr <= 0:
        raise ValueError("signal_atr debe ser mayor que 0")

    return entry_open - signal_close > threshold_atr * signal_atr
