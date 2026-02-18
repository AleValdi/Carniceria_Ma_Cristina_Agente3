"""
Resolucion de CantidadNeta: matchea productos de facturas XML
contra datos del Google Sheet por DIA y PRODUCTO.
"""
from decimal import Decimal
from typing import Dict, List, Tuple, Optional

from loguru import logger

from src.sat.models import Factura
from src.erp.models import ResultadoMatchProducto
from src.matching.cache_productos import CacheProductos, normalizar_texto


class CantidadNetaResolver:
    """
    Resuelve CantidadNeta para cada concepto de una factura
    usando los datos del Google Sheet.

    Algoritmo de matching por concepto:
    1. PRIORIDAD 1: Match directo por nombre XML normalizado
    2. PRIORIDAD 2: Match por nombre ERP (del pipeline de matching existente)
    3. PRIORIDAD 3: Match por codigo SAT unico (buscar_unico_por_codigo_sat)
    4. Si ninguno matchea: CantidadNeta = 0

    El match requiere coincidencia de AMBOS: DIA (fecha) Y PRODUCTO (nombre).
    """

    def __init__(
        self,
        sheets_index: Dict[Tuple[str, str], Decimal],
        cache_productos: CacheProductos,
    ):
        """
        Args:
            sheets_index: Indice del Google Sheet: {(fecha_str, producto_norm) -> cantidad_neta}
            cache_productos: Cache del catalogo ERP (para buscar_unico_por_codigo_sat)
        """
        self.sheets_index = sheets_index
        self.cache = cache_productos

    def resolver_para_factura(
        self,
        factura: Factura,
        matches: List[ResultadoMatchProducto],
    ) -> None:
        """
        Resuelve CantidadNeta para cada match de la factura.
        Modifica match.cantidad_neta in-place.

        Args:
            factura: Factura CFDI parseada
            matches: Lista de resultados de matching (se modifican in-place)
        """
        if not self.sheets_index:
            logger.debug("Indice de Google Sheets vacio, CantidadNeta sera 0 para todos")
            return

        fecha_key = factura.fecha_emision.strftime('%Y-%m-%d')
        resueltos = 0
        total = len(matches)

        for match in matches:
            concepto = match.concepto_xml
            cantidad = self._resolver_concepto(fecha_key, concepto, match)

            if cantidad > Decimal('0'):
                match.cantidad_neta = cantidad
                resueltos += 1
                logger.debug(
                    f"  CantidadNeta resuelta: {concepto.descripcion[:40]} -> {cantidad}"
                )

        if resueltos > 0:
            logger.info(
                f"CantidadNeta: {resueltos}/{total} conceptos resueltos "
                f"para factura {factura.uuid[:12]}..."
            )
        else:
            logger.debug(
                f"CantidadNeta: 0/{total} conceptos resueltos "
                f"para factura {factura.uuid[:12]}..."
            )

    def _resolver_concepto(
        self,
        fecha_key: str,
        concepto,
        match: ResultadoMatchProducto,
    ) -> Decimal:
        """
        Intenta resolver CantidadNeta para un concepto individual.

        Args:
            fecha_key: Fecha en formato YYYY-MM-DD
            concepto: Concepto XML
            match: Resultado de matching del pipeline

        Returns:
            Decimal con la cantidad neta o Decimal('0') si no se encontro
        """
        # PRIORIDAD 1: Match directo por nombre del XML
        nombre_xml = normalizar_texto(concepto.descripcion)
        if nombre_xml:
            cantidad = self._buscar_en_sheets(fecha_key, nombre_xml)
            if cantidad is not None:
                return cantidad

        # PRIORIDAD 2: Match por nombre ERP (resultado del pipeline de matching)
        if match.producto_erp:
            nombre_erp = normalizar_texto(match.producto_erp.nombre)
            if nombre_erp and nombre_erp != nombre_xml:
                cantidad = self._buscar_en_sheets(fecha_key, nombre_erp)
                if cantidad is not None:
                    return cantidad

        # PRIORIDAD 3: Match por codigo SAT unico (fallback)
        if not match.producto_erp:
            producto_sat = self.cache.buscar_unico_por_codigo_sat(
                concepto.clave_prod_serv
            )
            if producto_sat:
                nombre_sat = normalizar_texto(producto_sat.nombre)
                if nombre_sat and nombre_sat != nombre_xml:
                    cantidad = self._buscar_en_sheets(fecha_key, nombre_sat)
                    if cantidad is not None:
                        return cantidad

        return Decimal('0')

    def _buscar_en_sheets(
        self,
        fecha_key: str,
        producto_norm: str,
    ) -> Optional[Decimal]:
        """
        Buscar cantidad neta en el indice del Google Sheet.

        Args:
            fecha_key: Fecha en formato YYYY-MM-DD
            producto_norm: Nombre de producto normalizado

        Returns:
            Decimal con cantidad neta si se encontro match, None si no
        """
        clave = (fecha_key, producto_norm)
        cantidad = self.sheets_index.get(clave)
        if cantidad is not None and cantidad > Decimal('0'):
            return cantidad
        return None
