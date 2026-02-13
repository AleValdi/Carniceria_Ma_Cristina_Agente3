"""
Repositorio de facturas Serie F para vinculacion con Notas de Credito.
Busca facturas en SAVRecC por UUID para asociar NCs de proveedores.
"""
from typing import Optional, Tuple
from loguru import logger

from config.settings import sav7_config
from src.erp.sav7_connector import SAV7Connector
from src.erp.models import FacturaVinculada


SERIE_FACTURA = 'F'


class FacturaRepository:
    """Repositorio para buscar facturas Serie F en SAVRecC"""

    def __init__(self, connector: Optional[SAV7Connector] = None):
        self.connector = connector or SAV7Connector()
        self.config = sav7_config

    def buscar_por_uuid(self, uuid: str) -> Optional[FacturaVinculada]:
        """
        Buscar una factura Serie F por su UUID (TimbradoFolioFiscal).

        Args:
            uuid: UUID del CFDI a buscar (se normaliza a UPPERCASE)

        Returns:
            FacturaVinculada si se encuentra, None si no
        """
        query = f"""
            SELECT Serie, NumRec, Factura, Fecha, Total, Saldo,
                   ISNULL(NCredito, 0) as NCredito,
                   ISNULL(Pagado, 0) as Pagado,
                   Estatus, TimbradoFolioFiscal, Proveedor,
                   SubTotal1, Iva
            FROM {self.config.tabla_recepciones}
            WHERE TimbradoFolioFiscal = ? AND Serie = ?
        """
        resultados = self.connector.execute_custom_query(
            query, (uuid.upper(), SERIE_FACTURA)
        )

        if not resultados:
            logger.warning(f"Factura no encontrada por UUID: {uuid[:12]}...")
            return None

        row = resultados[0]
        factura = FacturaVinculada(
            serie=row['Serie'],
            num_rec=row['NumRec'],
            factura=row.get('Factura', '') or '',
            fecha=row['Fecha'],
            total=float(row['Total'] or 0),
            saldo=float(row['Saldo'] or 0),
            ncredito_acumulado=float(row['NCredito'] or 0),
            pagado=float(row['Pagado'] or 0),
            estatus=row.get('Estatus', '') or '',
            uuid=row.get('TimbradoFolioFiscal', '') or '',
            proveedor=row.get('Proveedor', '') or '',
            subtotal=float(row.get('SubTotal1', 0) or 0),
            iva=float(row.get('Iva', 0) or 0),
        )

        logger.info(
            f"Factura encontrada: F-{factura.num_rec} | "
            f"Total=${factura.total:,.2f} | Saldo=${factura.saldo:,.2f} | "
            f"Estatus={factura.estatus}"
        )
        return factura

    def validar_para_nc(
        self,
        factura: FacturaVinculada,
        monto_nc: float,
        clave_proveedor: str,
    ) -> Tuple[bool, str]:
        """
        Validar que una factura es apta para recibir una nota de credito.

        Validaciones:
        1. Factura existe (ya verificado al llegar aqui)
        2. Proveedor coincide
        3. Estatus permite NC (No Pagada o similar)
        4. Saldo > 0

        Args:
            factura: FacturaVinculada encontrada
            monto_nc: Monto total de la nota de credito
            clave_proveedor: Clave del proveedor del CFDI Egreso

        Returns:
            Tupla (es_valida, mensaje_error)
        """
        # Verificar proveedor
        if factura.proveedor != clave_proveedor:
            return False, (
                f"Proveedor no coincide: factura={factura.proveedor}, "
                f"NC={clave_proveedor}"
            )

        # Verificar estatus (aceptar No Pagada como minimo)
        estatus_validos = ['No Pagada']
        if factura.estatus not in estatus_validos:
            return False, (
                f"Estatus de factura no permite NC: '{factura.estatus}' "
                f"(esperado: {estatus_validos})"
            )

        # Verificar saldo > 0
        if factura.saldo <= 0:
            return False, (
                f"Factura sin saldo pendiente: Saldo=${factura.saldo:,.2f}"
            )

        # Advertencia si el monto NC supera el saldo (no bloquea, solo advierte)
        if monto_nc > factura.saldo:
            logger.warning(
                f"Monto NC (${monto_nc:,.2f}) supera saldo de factura "
                f"(${factura.saldo:,.2f}). Se registrara de todas formas."
            )

        return True, ""
