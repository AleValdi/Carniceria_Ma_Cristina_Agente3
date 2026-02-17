"""
Repositorio de proveedores - Busqueda en SAVProveedor
"""
from typing import Optional
from loguru import logger

from config.settings import sav7_config
from src.erp.sav7_connector import SAV7Connector
from src.erp.models import ProveedorERP


class ProveedorRepository:
    """Repositorio para buscar proveedores en SAVProveedor"""

    def __init__(self, connector: Optional[SAV7Connector] = None):
        self.connector = connector or SAV7Connector()
        self.config = sav7_config

    def buscar_por_rfc(self, rfc: str) -> Optional[ProveedorERP]:
        """
        Buscar proveedor por RFC en SAVProveedor.

        Args:
            rfc: RFC del proveedor a buscar

        Returns:
            ProveedorERP si se encuentra, None si no existe
        """
        rfc = rfc.strip().upper()

        query = f"""
            SELECT Clave, Empresa, RFC, Ciudad, Estado, Tipo, Plazo,
                   Direccion, Colonia, CP, Pais
            FROM {self.config.tabla_proveedores}
            WHERE RFC = ?
            ORDER BY Clave DESC
        """

        try:
            resultados = self.connector.execute_custom_query(query, (rfc,))

            if not resultados:
                logger.warning(f"Proveedor no encontrado para RFC: {rfc}")
                return None

            row = resultados[0]
            proveedor = ProveedorERP(
                clave=row['Clave'].strip(),
                empresa=row['Empresa'].strip() if row['Empresa'] else '',
                rfc=row['RFC'].strip() if row['RFC'] else '',
                ciudad=row.get('Ciudad', '').strip() if row.get('Ciudad') else 'NO ASIGNADA',
                estado=row.get('Estado', '').strip() if row.get('Estado') else 'NO ASIGNADO',
                tipo=row.get('Tipo', '').strip() if row.get('Tipo') else 'NACIONAL',
                plazo=row.get('Plazo', 0) or 0,
                direccion=row.get('Direccion', '').strip() if row.get('Direccion') else '',
                colonia=row.get('Colonia', '').strip() if row.get('Colonia') else '',
                cp=row.get('CP', '').strip() if row.get('CP') else '',
                pais=row.get('Pais', '').strip() if row.get('Pais') else 'MÉXICO',
            )

            logger.debug(f"Proveedor encontrado: {proveedor.clave} - {proveedor.empresa}")
            return proveedor

        except Exception as e:
            logger.error(f"Error al buscar proveedor por RFC {rfc}: {e}")
            raise
