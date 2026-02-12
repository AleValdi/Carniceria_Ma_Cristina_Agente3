"""
Historial de compras por proveedor.
Consulta la BD para obtener los productos que un proveedor ha vendido historicamente.
"""
from typing import List, Dict, Optional
from loguru import logger

from config.settings import sav7_config
from src.erp.sav7_connector import SAV7Connector
from src.erp.models import ProductoERP
from src.matching.cache_productos import CacheProductos


class HistorialCompras:
    """
    Consulta el historial de compras para obtener productos
    que un proveedor ha vendido historicamente.
    Esto reduce drasticamente el espacio de busqueda del matching.
    """

    def __init__(
        self,
        connector: Optional[SAV7Connector] = None,
        cache: Optional[CacheProductos] = None
    ):
        self.connector = connector or SAV7Connector()
        self.config = sav7_config
        self.cache = cache
        # Cache interno de historial por proveedor
        self._historial_cache: Dict[str, List[ProductoERP]] = {}

    def obtener_productos_proveedor(self, clave_proveedor: str) -> List[ProductoERP]:
        """
        Obtener productos que un proveedor ha vendido historicamente.
        Consulta SAVRecD + SAVRecC para encontrar los productos mas frecuentes.

        Args:
            clave_proveedor: Clave del proveedor en SAVProveedor

        Returns:
            Lista de ProductoERP ordenados por frecuencia de compra (mas frecuente primero)
        """
        # Revisar cache interno
        if clave_proveedor in self._historial_cache:
            return self._historial_cache[clave_proveedor]

        query = f"""
            SELECT d.Producto, d.Nombre, COUNT(*) as veces_comprado
            FROM {self.config.tabla_detalle_recepciones} d
            INNER JOIN {self.config.tabla_recepciones} c
                ON d.Serie = c.Serie AND d.NumRec = c.NumRec
            WHERE c.Proveedor = ?
              AND c.Serie IN ('R', 'F')
              AND d.Producto IS NOT NULL
              AND d.Producto != ''
            GROUP BY d.Producto, d.Nombre
            ORDER BY veces_comprado DESC
        """

        try:
            resultados = self.connector.execute_custom_query(query, (clave_proveedor,))

            productos = []
            for row in resultados:
                codigo = row['Producto'].strip() if row['Producto'] else ''
                if not codigo:
                    continue

                # Buscar en cache de productos para obtener datos completos
                if self.cache:
                    producto = self.cache.buscar_por_codigo(codigo)
                    if producto:
                        productos.append(producto)

            # Guardar en cache interno
            self._historial_cache[clave_proveedor] = productos

            logger.debug(
                f"Historial proveedor {clave_proveedor}: {len(productos)} productos distintos"
            )
            return productos

        except Exception as e:
            logger.error(f"Error al obtener historial de proveedor {clave_proveedor}: {e}")
            return []

    def limpiar_cache(self):
        """Limpiar cache interno de historial"""
        self._historial_cache.clear()
