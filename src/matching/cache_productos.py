"""
Cache en memoria del catalogo de productos para busqueda rapida
"""
import unicodedata
from typing import Dict, List, Optional
from loguru import logger

from src.erp.models import ProductoERP


def normalizar_texto(texto: str) -> str:
    """
    Normalizar texto para comparacion.
    Convierte a mayusculas, quita acentos, tabs y espacios multiples.
    """
    if not texto:
        return ''
    texto = texto.upper().strip()
    # Quitar tabs (encontrados en algunos nombres de productos)
    texto = texto.replace('\t', ' ')
    # Quitar acentos
    texto = unicodedata.normalize('NFKD', texto)
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    # Colapsar espacios multiples
    texto = ' '.join(texto.split())
    return texto


class CacheProductos:
    """
    Cache en memoria del catalogo de productos.
    Carga todos los productos una sola vez y crea indices
    para busqueda rapida por nombre y codigo SAT.
    """

    def __init__(self):
        self._por_nombre: Dict[str, ProductoERP] = {}
        self._por_codigo: Dict[str, ProductoERP] = {}
        self._por_codigo_sat: Dict[str, List[ProductoERP]] = {}
        self._todos: List[ProductoERP] = []
        self._nombres_normalizados: Dict[str, str] = {}  # codigo -> nombre normalizado

    def cargar(self, productos: List[ProductoERP]):
        """
        Indexar todos los productos para busqueda rapida.

        Args:
            productos: Lista de productos del catalogo ERP
        """
        self._todos = productos
        self._por_nombre.clear()
        self._por_codigo.clear()
        self._por_codigo_sat.clear()
        self._nombres_normalizados.clear()

        for producto in productos:
            nombre_norm = normalizar_texto(producto.nombre)

            # Indice por nombre normalizado (solo guarda el primero si hay duplicados)
            if nombre_norm and nombre_norm not in self._por_nombre:
                self._por_nombre[nombre_norm] = producto

            # Indice por codigo
            if producto.codigo:
                self._por_codigo[producto.codigo] = producto

            # Indice por codigo SAT
            if producto.codigo_sat and producto.codigo_sat != '01010101':
                if producto.codigo_sat not in self._por_codigo_sat:
                    self._por_codigo_sat[producto.codigo_sat] = []
                self._por_codigo_sat[producto.codigo_sat].append(producto)

            # Cache de nombres normalizados
            self._nombres_normalizados[producto.codigo] = nombre_norm

        logger.info(
            f"Cache cargado: {len(self._todos)} productos, "
            f"{len(self._por_nombre)} nombres unicos, "
            f"{len(self._por_codigo_sat)} codigos SAT"
        )

    def buscar_exacto(self, nombre: str) -> Optional[ProductoERP]:
        """
        Busqueda O(1) por nombre normalizado exacto.

        Args:
            nombre: Nombre del producto a buscar

        Returns:
            ProductoERP si hay match exacto, None si no
        """
        nombre_norm = normalizar_texto(nombre)
        return self._por_nombre.get(nombre_norm)

    def buscar_por_codigo_sat(self, codigo_sat: str) -> List[ProductoERP]:
        """
        Busqueda O(1) por codigo SAT.

        Args:
            codigo_sat: ClaveProdServ del SAT

        Returns:
            Lista de productos con ese codigo SAT
        """
        if not codigo_sat or codigo_sat == '01010101':
            return []
        return self._por_codigo_sat.get(codigo_sat, [])

    def buscar_por_codigo(self, codigo: str) -> Optional[ProductoERP]:
        """
        Busqueda O(1) por codigo de producto ERP.

        Args:
            codigo: Codigo del producto (ej: FYV002011)

        Returns:
            ProductoERP si existe, None si no
        """
        return self._por_codigo.get(codigo)

    def obtener_nombre_normalizado(self, codigo: str) -> str:
        """Obtener nombre normalizado de un producto por su codigo"""
        return self._nombres_normalizados.get(codigo, '')

    def obtener_todos(self) -> List[ProductoERP]:
        """Obtener la lista completa de productos"""
        return self._todos

    @property
    def total_productos(self) -> int:
        """Total de productos en el cache"""
        return len(self._todos)
