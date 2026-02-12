"""
Algoritmo de matching de conceptos XML contra catalogo de productos ERP.

Pipeline de 4 pasos:
1. Match exacto por nombre normalizado
2. Historial del proveedor + fuzzy matching
3. Codigo SAT + fuzzy matching
4. Catalogo completo + fuzzy matching (threshold mas estricto)
"""
from typing import List, Optional, Tuple
from rapidfuzz import fuzz
from loguru import logger

from config.settings import settings
from src.sat.models import Concepto
from src.erp.models import ProductoERP, ResultadoMatchProducto
from src.matching.cache_productos import CacheProductos, normalizar_texto
from src.matching.historial_compras import HistorialCompras


class ProductoMatcher:
    """
    Motor de matching de conceptos XML contra catalogo ERP.
    Usa una cascada de metodos desde el mas preciso al mas general.
    """

    def __init__(
        self,
        cache: CacheProductos,
        historial: HistorialCompras
    ):
        self.cache = cache
        self.historial = historial
        self.umbral_match = settings.umbral_match_producto  # 90
        self.umbral_match_global = settings.umbral_match_exacto  # 95
        self.margen_ambiguedad = 5  # Puntos de diferencia para considerar ambiguo

    def matchear_concepto(
        self,
        concepto: Concepto,
        clave_proveedor: str
    ) -> ResultadoMatchProducto:
        """
        Intentar matchear un concepto XML con un producto del catalogo ERP.
        Ejecuta la cascada de matching en orden de precision.

        Args:
            concepto: Concepto del XML CFDI
            clave_proveedor: Clave del proveedor para filtrar por historial

        Returns:
            ResultadoMatchProducto con el resultado del matching
        """
        descripcion_xml = concepto.descripcion
        nombre_normalizado = normalizar_texto(descripcion_xml)

        if not nombre_normalizado:
            return ResultadoMatchProducto(
                concepto_xml=concepto,
                mensaje="Descripcion vacia en el XML"
            )

        # Paso 1: Match exacto por nombre
        resultado = self._match_exacto(concepto, nombre_normalizado)
        if resultado.matcheado:
            return resultado

        # Paso 2: Historial del proveedor + fuzzy (token_sort_ratio)
        resultado = self._match_historial_proveedor(
            concepto, nombre_normalizado, clave_proveedor
        )
        if resultado.matcheado:
            return resultado

        # Paso 2.5: Historial del proveedor + token_set_ratio + frecuencia
        if settings.habilitar_token_set_historial:
            resultado = self._match_historial_token_set(
                concepto, nombre_normalizado, clave_proveedor
            )
            if resultado.matcheado:
                return resultado

        # Paso 3: Codigo SAT + fuzzy
        resultado = self._match_codigo_sat(concepto, nombre_normalizado)
        if resultado.matcheado:
            return resultado

        # Paso 4: Catalogo completo + fuzzy (threshold mas estricto)
        resultado = self._match_catalogo_completo(concepto, nombre_normalizado)
        if resultado.matcheado:
            return resultado

        # Sin match
        return ResultadoMatchProducto(
            concepto_xml=concepto,
            mensaje=f"No se encontro match para: {descripcion_xml}"
        )

    def matchear_lote(
        self,
        conceptos: List[Concepto],
        clave_proveedor: str
    ) -> List[ResultadoMatchProducto]:
        """
        Matchear una lista de conceptos.

        Args:
            conceptos: Lista de conceptos del XML
            clave_proveedor: Clave del proveedor

        Returns:
            Lista de ResultadoMatchProducto
        """
        resultados = []
        for concepto in conceptos:
            resultado = self.matchear_concepto(concepto, clave_proveedor)
            resultados.append(resultado)

            nivel = resultado.nivel_confianza
            if resultado.matcheado:
                logger.debug(
                    f"  [{nivel}] '{concepto.descripcion}' -> "
                    f"'{resultado.producto_erp.nombre}' ({resultado.producto_erp.codigo}) "
                    f"via {resultado.metodo_match} ({resultado.confianza:.0%})"
                )
            else:
                logger.debug(
                    f"  [NO_MATCH] '{concepto.descripcion}' - {resultado.mensaje}"
                )

        matcheados = sum(1 for r in resultados if r.matcheado)
        logger.info(
            f"Matching completado: {matcheados}/{len(conceptos)} conceptos matcheados"
        )

        return resultados

    def _match_exacto(
        self,
        concepto: Concepto,
        nombre_normalizado: str
    ) -> ResultadoMatchProducto:
        """Paso 1: Busqueda exacta por nombre normalizado"""
        producto = self.cache.buscar_exacto(nombre_normalizado)

        if producto:
            return ResultadoMatchProducto(
                concepto_xml=concepto,
                producto_erp=producto,
                confianza=1.0,
                nivel_confianza="EXACTO",
                metodo_match="exacto",
                mensaje=f"Match exacto: {producto.codigo}"
            )

        return ResultadoMatchProducto(concepto_xml=concepto)

    def _match_historial_proveedor(
        self,
        concepto: Concepto,
        nombre_normalizado: str,
        clave_proveedor: str
    ) -> ResultadoMatchProducto:
        """Paso 2: Busqueda en historial del proveedor + fuzzy"""
        productos_historial = self.historial.obtener_productos_proveedor(clave_proveedor)

        if not productos_historial:
            return ResultadoMatchProducto(concepto_xml=concepto)

        mejor, candidatos = self._fuzzy_match_lista(
            nombre_normalizado, productos_historial, self.umbral_match
        )

        if mejor:
            producto, score = mejor
            return ResultadoMatchProducto(
                concepto_xml=concepto,
                producto_erp=producto,
                confianza=score / 100.0,
                nivel_confianza="ALTA",
                metodo_match="historial",
                candidatos_descartados=candidatos,
                mensaje=f"Match historial proveedor: {producto.codigo} (score {score})"
            )

        return ResultadoMatchProducto(concepto_xml=concepto)

    def _match_codigo_sat(
        self,
        concepto: Concepto,
        nombre_normalizado: str
    ) -> ResultadoMatchProducto:
        """Paso 3: Filtrar por codigo SAT + fuzzy"""
        codigo_sat = concepto.clave_prod_serv
        productos_sat = self.cache.buscar_por_codigo_sat(codigo_sat)

        if not productos_sat:
            return ResultadoMatchProducto(concepto_xml=concepto)

        mejor, candidatos = self._fuzzy_match_lista(
            nombre_normalizado, productos_sat, self.umbral_match
        )

        if mejor:
            producto, score = mejor
            return ResultadoMatchProducto(
                concepto_xml=concepto,
                producto_erp=producto,
                confianza=score / 100.0,
                nivel_confianza="ALTA",
                metodo_match="codigo_sat",
                candidatos_descartados=candidatos,
                mensaje=f"Match codigo SAT {codigo_sat}: {producto.codigo} (score {score})"
            )

        return ResultadoMatchProducto(concepto_xml=concepto)

    def _match_catalogo_completo(
        self,
        concepto: Concepto,
        nombre_normalizado: str
    ) -> ResultadoMatchProducto:
        """Paso 4: Busqueda en catalogo completo con threshold estricto"""
        todos = self.cache.obtener_todos()

        mejor, candidatos = self._fuzzy_match_lista(
            nombre_normalizado, todos, self.umbral_match_global
        )

        if mejor:
            producto, score = mejor
            return ResultadoMatchProducto(
                concepto_xml=concepto,
                producto_erp=producto,
                confianza=score / 100.0,
                nivel_confianza="MEDIA",
                metodo_match="fuzzy_global",
                candidatos_descartados=candidatos,
                mensaje=f"Match catalogo completo: {producto.codigo} (score {score})"
            )

        return ResultadoMatchProducto(concepto_xml=concepto)

    def _match_historial_token_set(
        self,
        concepto: Concepto,
        nombre_normalizado: str,
        clave_proveedor: str
    ) -> ResultadoMatchProducto:
        """
        Paso 2.5: Historial del proveedor + token_set_ratio + desambiguacion por frecuencia.
        Maneja nombres abreviados (ej: 'JALAPENO' vs 'CHILE JALAPENO VERDE').
        """
        # Guard: rechazar nombres muy cortos
        if len(nombre_normalizado) < settings.min_longitud_token_set:
            return ResultadoMatchProducto(concepto_xml=concepto)

        productos_freq = self.historial.obtener_productos_con_frecuencia(clave_proveedor)
        if not productos_freq:
            return ResultadoMatchProducto(concepto_xml=concepto)

        mejor, candidatos = self._fuzzy_match_token_set(
            nombre_normalizado, productos_freq, self.umbral_match
        )

        if mejor:
            producto, score = mejor
            return ResultadoMatchProducto(
                concepto_xml=concepto,
                producto_erp=producto,
                confianza=score / 100.0,
                nivel_confianza="ALTA",
                metodo_match="historial_token_set",
                candidatos_descartados=candidatos,
                mensaje=f"Match historial token_set: {producto.codigo} (score {score}, freq {producto.frecuencia})"
            )

        return ResultadoMatchProducto(concepto_xml=concepto)

    def _fuzzy_match_token_set(
        self,
        nombre_normalizado: str,
        productos: List[ProductoERP],
        umbral: int
    ) -> Tuple[Optional[Tuple[ProductoERP, int]], int]:
        """
        Fuzzy matching usando token_set_ratio con desambiguacion por frecuencia.
        token_set_ratio maneja bien nombres abreviados porque compara interseccion de tokens.

        Desambiguacion: si top 2 empatan (gap < margen_ambiguedad), acepta solo si
        la frecuencia del mejor es >= 2x la del segundo. Si no, rechaza como ambiguo.
        """
        candidatos: List[Tuple[ProductoERP, int]] = []

        for producto in productos:
            nombre_prod = self.cache.obtener_nombre_normalizado(producto.codigo)
            if not nombre_prod:
                nombre_prod = normalizar_texto(producto.nombre)

            score = fuzz.token_set_ratio(nombre_normalizado, nombre_prod)

            if score >= umbral:
                candidatos.append((producto, score))

        if not candidatos:
            return None, 0

        # Ordenar por score DESC, luego por frecuencia DESC
        candidatos.sort(key=lambda x: (x[1], x[0].frecuencia), reverse=True)

        mejor_producto, mejor_score = candidatos[0]

        # Detectar ambiguedad con desambiguacion por frecuencia
        if len(candidatos) > 1:
            segundo_producto, segundo_score = candidatos[1]
            gap = mejor_score - segundo_score

            if gap < self.margen_ambiguedad:
                # Empate: intentar desambiguar por frecuencia
                freq_mejor = mejor_producto.frecuencia
                freq_segundo = segundo_producto.frecuencia

                if freq_mejor > 0 and freq_segundo > 0:
                    if freq_mejor >= 2 * freq_segundo:
                        # Frecuencia del mejor es >= 2x, aceptar
                        logger.info(
                            f"Token_set desambiguado por frecuencia: "
                            f"'{mejor_producto.nombre}' (score={mejor_score}, freq={freq_mejor}) vs "
                            f"'{segundo_producto.nombre}' (score={segundo_score}, freq={freq_segundo})"
                        )
                        return (mejor_producto, mejor_score), len(candidatos) - 1

                # No se puede desambiguar: rechazar
                logger.warning(
                    f"Token_set ambiguo para '{nombre_normalizado}': "
                    f"{mejor_producto.nombre} ({mejor_score}, freq={mejor_producto.frecuencia}) vs "
                    f"{segundo_producto.nombre} ({segundo_score}, freq={segundo_producto.frecuencia})"
                )
                return None, len(candidatos)

        # Sin ambiguedad, pero verificar que tiene frecuencia
        if mejor_producto.frecuencia == 0:
            logger.debug(
                f"Token_set sin frecuencia para '{nombre_normalizado}': "
                f"{mejor_producto.nombre} ({mejor_score})"
            )
            return None, len(candidatos)

        return (mejor_producto, mejor_score), len(candidatos) - 1

    def _fuzzy_match_lista(
        self,
        nombre_normalizado: str,
        productos: List[ProductoERP],
        umbral: int
    ) -> Tuple[Optional[Tuple[ProductoERP, int]], int]:
        """
        Realizar fuzzy matching contra una lista de productos.
        Detecta ambiguedad (multiples candidatos con score similar).

        Args:
            nombre_normalizado: Nombre a buscar (ya normalizado)
            productos: Lista de productos candidatos
            umbral: Score minimo para aceptar

        Returns:
            Tupla de:
            - (ProductoERP, score) si hay match claro, None si no
            - Cantidad de candidatos descartados
        """
        candidatos: List[Tuple[ProductoERP, int]] = []

        for producto in productos:
            nombre_prod = self.cache.obtener_nombre_normalizado(producto.codigo)
            if not nombre_prod:
                nombre_prod = normalizar_texto(producto.nombre)

            score = fuzz.token_sort_ratio(nombre_normalizado, nombre_prod)

            if score >= umbral:
                candidatos.append((producto, score))

        if not candidatos:
            return None, 0

        # Ordenar por score descendente
        candidatos.sort(key=lambda x: x[1], reverse=True)

        mejor_producto, mejor_score = candidatos[0]

        # Detectar ambiguedad: si hay mas de un candidato y el segundo
        # esta a menos de margen_ambiguedad puntos del primero, es ambiguo
        if len(candidatos) > 1:
            segundo_score = candidatos[1][1]
            if mejor_score - segundo_score < self.margen_ambiguedad:
                logger.warning(
                    f"Match ambiguo para '{nombre_normalizado}': "
                    f"{mejor_producto.nombre} ({mejor_score}) vs "
                    f"{candidatos[1][0].nombre} ({segundo_score})"
                )
                return None, len(candidatos)

        return (mejor_producto, mejor_score), len(candidatos) - 1
