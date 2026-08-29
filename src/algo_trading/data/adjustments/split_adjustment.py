from collections.abc import Iterable
from datetime import date
from decimal import Decimal


class SplitAdjustmentCalculator:
    """Calcula factores históricos de ajuste por splits."""

    @staticmethod
    def factor_for_bar(
        bar_date: date,
        ex_date: date,
        split_ratio: Decimal,
    ) -> Decimal:
        """
        Devuelve el factor aplicable a una barra.

        Las barras anteriores a ex_date se ajustan.
        Las barras de ex_date y posteriores no se ajustan.
        """
        if split_ratio <= Decimal("0"):
            raise ValueError("split_ratio debe ser mayor que cero")

        if bar_date >= ex_date:
            return Decimal("1")

        return Decimal("1") / split_ratio

    @staticmethod
    def adjust_price(price: Decimal, factor: Decimal) -> Decimal:
        if price <= Decimal("0"):
            raise ValueError("El precio debe ser mayor que cero")

        if factor <= Decimal("0"):
            raise ValueError("El factor debe ser mayor que cero")

        return price * factor

    @staticmethod
    def adjust_volume(volume: Decimal, factor: Decimal) -> Decimal:
        if volume < Decimal("0"):
            raise ValueError("El volumen no puede ser negativo")

        if factor <= Decimal("0"):
            raise ValueError("El factor debe ser mayor que cero")

        return volume / factor

    @staticmethod
    def cumulative_factor_for_bar(
        bar_date: date,
        splits: Iterable[tuple[date, Decimal]],
    ) -> Decimal:
        """
        Calcula el factor acumulado de todos los splits posteriores a la barra.

        Cada elemento de splits debe ser:
            (ex_date, split_ratio)

        Por ejemplo, un split 2:1 se representa como:
            (date(2024, 1, 2), Decimal("2"))
        """
        factor = Decimal("1")

        for ex_date, split_ratio in splits:
            if split_ratio <= Decimal("0"):
                raise ValueError("split_ratio debe ser mayor que cero")

            if bar_date < ex_date:
                factor /= split_ratio

        return factor
