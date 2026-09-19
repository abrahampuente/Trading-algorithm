from sqlalchemy.orm import Session

from algo_trading.data.market_data_snapshot_persistence_service import (
    MarketDataSnapshotPersistenceService,
)
from algo_trading.data.providers.dto import (
    CorporateActionRequest,
    DataRequest,
    DataSnapshot,
    MarketDataSnapshot,
)
from algo_trading.data.providers.protocol import MarketDataProvider


class InconsistentSnapshotRequestError(ValueError):
    """Indica que las solicitudes no pueden formar un snapshot coherente."""


class ProviderSnapshotValidationError(ValueError):
    """Indica que el proveedor devolvió datos incompatibles con la solicitud."""


class MarketDataSnapshotIngestionService:
    """
    Descarga y persiste un snapshot completo de datos de mercado.

    El proveedor obtiene los datos; este servicio verifica que correspondan
    con las solicitudes y delega la escritura atómica en la capa de
    persistencia.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        session: Session,
    ) -> None:
        self._provider = provider
        self._persistence_service = MarketDataSnapshotPersistenceService(session)

    def ingest(
        self,
        data_request: DataRequest,
        corporate_action_request: CorporateActionRequest,
    ) -> DataSnapshot:
        """Obtiene, valida y persiste un snapshot completo."""
        self._validate_requests(data_request, corporate_action_request)

        snapshot = self._provider.get_snapshot(
            data_request=data_request,
            corporate_action_request=corporate_action_request,
        )

        self._validate_snapshot(
            snapshot=snapshot,
            data_request=data_request,
            corporate_action_request=corporate_action_request,
        )

        return self._persistence_service.persist(snapshot)

    @staticmethod
    def _validate_requests(
        data_request: DataRequest,
        corporate_action_request: CorporateActionRequest,
    ) -> None:
        if data_request.source != corporate_action_request.source:
            raise InconsistentSnapshotRequestError(
                "Las solicitudes deben utilizar la misma fuente de datos"
            )

        if set(data_request.symbols) != set(corporate_action_request.symbols):
            raise InconsistentSnapshotRequestError(
                "Las solicitudes deben incluir exactamente los mismos símbolos"
            )

        if data_request.start.date() != corporate_action_request.start:
            raise InconsistentSnapshotRequestError(
                "La fecha inicial de corporate actions debe coincidir "
                "con la fecha inicial de barras"
            )

        if data_request.end.date() != corporate_action_request.end:
            raise InconsistentSnapshotRequestError(
                "La fecha final de corporate actions debe coincidir "
                "con la fecha final de barras"
            )

    @staticmethod
    def _validate_snapshot(
        snapshot: MarketDataSnapshot,
        data_request: DataRequest,
        corporate_action_request: CorporateActionRequest,
    ) -> None:
        metadata = snapshot.metadata

        if metadata.source != data_request.source:
            raise ProviderSnapshotValidationError(
                "El source del snapshot no coincide con la solicitud"
            )

        if metadata.timeframe != data_request.timeframe:
            raise ProviderSnapshotValidationError(
                "El timeframe del snapshot no coincide con la solicitud"
            )

        for bar in snapshot.bars:
            if bar.symbol not in data_request.symbols:
                raise ProviderSnapshotValidationError(
                    "El proveedor devolvió una barra de un símbolo no solicitado"
                )

            if bar.source != data_request.source:
                raise ProviderSnapshotValidationError(
                    "El source de una barra no coincide con la solicitud"
                )

            if bar.timeframe != data_request.timeframe:
                raise ProviderSnapshotValidationError(
                    "El timeframe de una barra no coincide con la solicitud"
                )

            if not data_request.start <= bar.timestamp <= data_request.end:
                raise ProviderSnapshotValidationError(
                    "El proveedor devolvió una barra fuera del rango solicitado"
                )

        for split in snapshot.splits:
            if split.symbol not in corporate_action_request.symbols:
                raise ProviderSnapshotValidationError(
                    "El proveedor devolvió un split de un símbolo no solicitado"
                )

            if split.source != corporate_action_request.source:
                raise ProviderSnapshotValidationError(
                    "El source de un split no coincide con la solicitud"
                )

            if (
                not corporate_action_request.start
                <= split.ex_date
                <= (corporate_action_request.end)
            ):
                raise ProviderSnapshotValidationError(
                    "El proveedor devolvió un split fuera del rango solicitado"
                )

        for dividend in snapshot.dividends:
            if dividend.symbol not in corporate_action_request.symbols:
                raise ProviderSnapshotValidationError(
                    "El proveedor devolvió un dividendo de un símbolo no solicitado"
                )

            if dividend.source != corporate_action_request.source:
                raise ProviderSnapshotValidationError(
                    "El source de un dividendo no coincide con la solicitud"
                )

            if (
                not corporate_action_request.start
                <= dividend.ex_date
                <= (corporate_action_request.end)
            ):
                raise ProviderSnapshotValidationError(
                    "El proveedor devolvió un dividendo fuera del rango solicitado"
                )
