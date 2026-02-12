"""
Repositorio de productos - Carga del catalogo SAVProducto
"""
from typing import List, Optional
from decimal import Decimal
from loguru import logger

from config.settings import sav7_config
from src.erp.sav7_connector import SAV7Connector
from src.erp.models import ProductoERP


class ProductoRepository:
    """Repositorio para cargar y consultar el catalogo de productos"""

    def __init__(self, connector: Optional[SAV7Connector] = None):
        self.connector = connector or SAV7Connector()
        self.config = sav7_config

    def cargar_catalogo(self) -> List[ProductoERP]:
        """
        Cargar todos los productos del catalogo SAVProducto.

        Returns:
            Lista de ProductoERP con todos los productos activos
        """
        query = f"""
            SELECT
                Codigo, Nombre, Familia1Nombre, Familia2Nombre,
                Unidad, CodigoSAT, PorcIva, Servicio
            FROM {self.config.tabla_productos}
            WHERE Codigo IS NOT NULL AND Codigo != ''
        """

        try:
            resultados = self.connector.execute_custom_query(query)

            productos = []
            for row in resultados:
                producto = ProductoERP(
                    codigo=row['Codigo'].strip() if row['Codigo'] else '',
                    nombre=row['Nombre'].strip() if row['Nombre'] else '',
                    familia1=row.get('Familia1Nombre', '').strip() if row.get('Familia1Nombre') else '',
                    familia2=row.get('Familia2Nombre', '').strip() if row.get('Familia2Nombre') else '',
                    unidad=row.get('Unidad', 'PZA').strip() if row.get('Unidad') else 'PZA',
                    codigo_sat=row.get('CodigoSAT', '').strip() if row.get('CodigoSAT') else '',
                    porc_iva=Decimal(str(row.get('PorcIva', 0) or 0)),
                    servicio=bool(row.get('Servicio', False)),
                )
                productos.append(producto)

            logger.info(f"Catalogo cargado: {len(productos)} productos")
            return productos

        except Exception as e:
            logger.error(f"Error al cargar catalogo de productos: {e}")
            raise
