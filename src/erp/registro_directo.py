"""
Modulo de registro directo de facturas CFDI en SAV7 (sin remisiones).
Crea registros Serie F en SAVRecC y SAVRecD directamente desde datos del XML.
"""
from typing import List, Optional
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from loguru import logger

from config.settings import settings, sav7_config
from src.erp.sav7_connector import SAV7Connector
from src.erp.models import (
    ProductoERP, ProveedorERP, ResultadoRegistro,
    ConceptoRegistrado, ResultadoMatchProducto
)
from src.erp.utils import numero_a_letra
from src.sat.models import Factura
from src.cfdi.attachment_manager import AttachmentManager


SERIE_FACTURA = 'F'


class RegistradorDirecto:
    """
    Registra facturas CFDI directamente en SAV7 como Serie F,
    sin vincular remisiones.
    """

    def __init__(self, connector: Optional[SAV7Connector] = None):
        self.connector = connector or SAV7Connector()
        self.config = sav7_config

    def verificar_uuid_existe(self, uuid: str) -> bool:
        """
        Verificar si un UUID ya existe en la BD para prevenir duplicados.

        Args:
            uuid: UUID del CFDI a verificar

        Returns:
            True si ya existe, False si no
        """
        query = f"""
            SELECT COUNT(*) as cuenta
            FROM {self.config.tabla_recepciones}
            WHERE TimbradoFolioFiscal = ? AND Serie = ?
        """
        resultados = self.connector.execute_custom_query(
            query, (uuid.upper(), SERIE_FACTURA)
        )
        cuenta = resultados[0]['cuenta'] if resultados else 0
        return cuenta > 0

    def verificar_factura_duplicada(self, factura_sat: 'Factura') -> bool:
        """
        Verificar si una factura ya existe en la BD por RFC + Folio + Total + Fecha.
        Complementa verificar_uuid_existe() cuando TimbradoFolioFiscal va vacio.

        La combinacion RFC + Folio + Total + Fecha es unica en las ~66,000+
        facturas de produccion (verificado Feb 2026).

        Args:
            factura_sat: Factura CFDI parseada del XML

        Returns:
            True si ya existe una factura con los mismos datos, False si no
        """
        folio = factura_sat.folio or ''
        if not folio:
            return False  # Sin folio no se puede validar

        query = f"""
            SELECT COUNT(*) as cuenta
            FROM {self.config.tabla_recepciones}
            WHERE Serie = ?
              AND RFC = ?
              AND Factura = ?
              AND Total = ?
              AND Fecha = ?
        """
        resultados = self.connector.execute_custom_query(
            query, (
                SERIE_FACTURA,
                factura_sat.rfc_emisor,
                folio,
                float(factura_sat.total),
                factura_sat.fecha_emision,
            )
        )
        cuenta = resultados[0]['cuenta'] if resultados else 0
        return cuenta > 0

    def obtener_siguiente_numrec(self) -> int:
        """Obtener el siguiente NumRec disponible para Serie F (dry-run)"""
        query = f"""
            SELECT ISNULL(MAX(NumRec), 0) + 1 as SiguienteNum
            FROM {self.config.tabla_recepciones}
            WHERE Serie = ?
        """
        resultados = self.connector.execute_custom_query(query, (SERIE_FACTURA,))
        siguiente = resultados[0]['SiguienteNum'] if resultados else 1
        return max(siguiente, settings.numrec_rango_minimo)

    def registrar(
        self,
        factura_sat: Factura,
        proveedor: ProveedorERP,
        matches: List[ResultadoMatchProducto],
        dry_run: bool = False
    ) -> ResultadoRegistro:
        """
        Registrar una factura CFDI en el ERP como Serie F.

        Args:
            factura_sat: Factura CFDI parseada del XML
            proveedor: Proveedor encontrado en SAVProveedor
            matches: Lista de resultados de matching de productos
            dry_run: Si True, simula sin escribir en BD

        Returns:
            ResultadoRegistro con el resultado
        """
        uuid = factura_sat.uuid.upper()

        # Filtrar solo los matcheados
        matcheados = [m for m in matches if m.matcheado]
        no_matcheados = [m for m in matches if not m.matcheado]

        total_conceptos = len(matches)
        conceptos_ok = len(matcheados)

        # Verificar minimo de conceptos matcheados
        porcentaje = (conceptos_ok / total_conceptos * 100) if total_conceptos > 0 else 0
        if porcentaje < settings.min_conceptos_match_porcentaje:
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                conceptos_no_matcheados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        motivo_no_registro=m.mensaje
                    ) for m in no_matcheados
                ],
                mensaje=(
                    f"Insuficientes conceptos matcheados: {conceptos_ok}/{total_conceptos} "
                    f"({porcentaje:.0f}% < {settings.min_conceptos_match_porcentaje}%)"
                ),
                error="INSUFICIENTES_MATCHES"
            )

        # Verificar UUID no duplicado
        if self.verificar_uuid_existe(uuid):
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                mensaje=f"UUID ya existe en la BD: {uuid}",
                error="UUID_DUPLICADO"
            )

        # Verificar duplicado por RFC + Folio + Total + Fecha
        # (complementa UUID cuando TimbradoFolioFiscal va vacio)
        if self.verificar_factura_duplicada(factura_sat):
            folio = factura_sat.folio or ''
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                mensaje=(
                    f"Factura duplicada: RFC={factura_sat.rfc_emisor}, "
                    f"Folio={folio}, Total=${float(factura_sat.total):,.2f}"
                ),
                error="FACTURA_DUPLICADA"
            )

        if dry_run:
            logger.info(f"[DRY-RUN] Se registraria F con {conceptos_ok} conceptos para UUID {uuid}")
            return ResultadoRegistro(
                exito=True,
                factura_uuid=uuid,
                numero_factura_erp="F-DRYRUN",
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                registro_parcial=len(no_matcheados) > 0,
                conceptos_registrados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        producto_erp=m.producto_erp,
                        registrado=True,
                        confianza_match=m.confianza,
                        metodo_match=m.metodo_match,
                    ) for m in matcheados
                ],
                conceptos_no_matcheados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        motivo_no_registro=m.mensaje
                    ) for m in no_matcheados
                ],
                mensaje=f"[DRY-RUN] {conceptos_ok}/{total_conceptos} conceptos matcheados"
            )

        try:
            # Calcular totales solo de los conceptos matcheados
            subtotal_matcheados = sum(
                m.concepto_xml.importe for m in matcheados
            )
            iva_matcheados = sum(
                m.concepto_xml.impuesto_iva_importe or Decimal('0')
                for m in matcheados
            )
            total_matcheados = subtotal_matcheados + iva_matcheados

            # Si es registro parcial, usar totales de matcheados
            # Si es registro completo, usar totales del XML
            es_parcial = len(no_matcheados) > 0
            if es_parcial:
                subtotal = subtotal_matcheados
                iva = iva_matcheados
                total = total_matcheados
            else:
                subtotal = factura_sat.subtotal
                iva = factura_sat.iva_trasladado
                total = factura_sat.total

            # Ejecutar en transaccion unica: SELECT MAX+1 con lock + INSERTs
            # El UPDLOCK/HOLDLOCK previene que otro proceso obtenga el mismo
            # NumRec entre el SELECT y el INSERT (race condition detectada
            # en produccion con F-68949).
            with self.connector.db.get_cursor() as cursor:
                nuevo_num_rec = self._obtener_siguiente_numrec_con_lock(cursor)

                logger.info(
                    f"Registrando factura: UUID {uuid[:12]}... -> F-{nuevo_num_rec} "
                    f"({conceptos_ok}/{total_conceptos} conceptos)"
                )

                self._insertar_cabecera(
                    cursor, nuevo_num_rec, factura_sat, proveedor,
                    matcheados, no_matcheados, subtotal, iva, total
                )
                self._insertar_detalles(
                    cursor, nuevo_num_rec, proveedor, matcheados
                )

            logger.info(f"Registro exitoso: F-{nuevo_num_rec}")

            if factura_sat.archivo_xml:
                try:
                    am = AttachmentManager(self.connector)
                    am.adjuntar_factura(
                        xml_origen=Path(factura_sat.archivo_xml),
                        rfc_emisor=factura_sat.rfc_emisor,
                        num_rec=nuevo_num_rec,
                        fecha=factura_sat.fecha_emision,
                    )
                except Exception as e:
                    logger.warning(f"No se pudo adjuntar XML de F-{nuevo_num_rec}: {e}")

            return ResultadoRegistro(
                exito=True,
                factura_uuid=uuid,
                numero_factura_erp=f"F-{nuevo_num_rec}",
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                registro_parcial=es_parcial,
                conceptos_registrados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        producto_erp=m.producto_erp,
                        registrado=True,
                        confianza_match=m.confianza,
                        metodo_match=m.metodo_match,
                    ) for m in matcheados
                ],
                conceptos_no_matcheados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        motivo_no_registro=m.mensaje
                    ) for m in no_matcheados
                ],
                mensaje=f"Registrado F-{nuevo_num_rec}: {conceptos_ok}/{total_conceptos} conceptos"
            )

        except Exception as e:
            logger.error(f"Error al registrar factura {uuid}: {e}")
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                mensaje=f"Error en registro: {str(e)}",
                error=str(e)
            )

    def _obtener_siguiente_numrec_con_lock(self, cursor) -> int:
        """
        Obtener siguiente NumRec DENTRO de la transaccion actual con lock.

        Usa UPDLOCK + HOLDLOCK para bloquear las filas leidas hasta que
        la transaccion termine (commit o rollback). Esto previene que
        otro proceso (Agente 2, SAV7 manual) obtenga el mismo NumRec.

        Ademas, aplica rango reservado (settings.numrec_rango_minimo)
        para que el Agente 3 use numeros >= 900000, separados del rango
        normal del ERP (actualmente ~68,000).

        IMPORTANTE: Este metodo DEBE ejecutarse dentro de un
        'with self.connector.db.get_cursor() as cursor' que tambien
        contenga los INSERTs posteriores.
        """
        query = f"""
            SELECT ISNULL(MAX(NumRec), 0) + 1 as SiguienteNum
            FROM {self.config.tabla_recepciones} WITH (UPDLOCK, HOLDLOCK)
            WHERE Serie = ?
        """
        cursor.execute(query, (SERIE_FACTURA,))
        row = cursor.fetchone()
        siguiente = row[0] if row else 1
        return max(siguiente, settings.numrec_rango_minimo)

    def _insertar_cabecera(
        self,
        cursor,
        num_rec: int,
        factura_sat: Factura,
        proveedor: ProveedorERP,
        matcheados: List[ResultadoMatchProducto],
        no_matcheados: List[ResultadoMatchProducto],
        subtotal: Decimal,
        iva: Decimal,
        total: Decimal,
    ):
        """Insertar cabecera de factura en SAVRecC (Serie F)"""

        # Calcular articulos y partidas
        total_articulos = sum(
            m.concepto_xml.cantidad for m in matcheados
        )
        total_articulos = round(float(total_articulos))  # Redondear como produccion
        total_partidas = len(matcheados)

        # Total en letra
        total_letra = numero_a_letra(total)

        # Metodo de pago del XML
        metodo_pago = factura_sat.metodo_pago.value if factura_sat.metodo_pago else 'PPD'

        fecha_actual = datetime.now()

        query = f"""
            INSERT INTO {self.config.tabla_recepciones} (
                Serie, NumRec, Proveedor, ProveedorNombre, Fecha,
                Comprador, Procesada, FechaAlta, UltimoCambio, Estatus,
                SubTotal1, Iva, Total, Pagado, Referencia,
                Comentario, Moneda, Paridad, Tipo, Plazo,
                SubTotal2, Factura, Saldo, Capturo, CapturoCambio,
                Articulos, Partidas, ProcesadaFecha, IntContable,
                TipoRecepcion, Consolidacion, RFC, TimbradoFolioFiscal,
                FacturaFecha, Sucursal, Departamento, Afectacion,
                MetododePago, NumOC, TotalLetra, Ciudad, Estado,
                TipoProveedor, TotalPrecio, TotalRecibidoNeto, SerieRFC
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
        """

        params = (
            SERIE_FACTURA,                          # Serie
            num_rec,                                 # NumRec
            proveedor.clave,                         # Proveedor
            proveedor.empresa[:60],                  # ProveedorNombre
            factura_sat.fecha_emision,               # Fecha (del XML)
            settings.usuario_sistema,                # Comprador
            0,                                       # Procesada
            fecha_actual,                            # FechaAlta
            fecha_actual,                            # UltimoCambio
            settings.estatus_registro,               # Estatus
            float(subtotal),                         # SubTotal1
            float(iva),                              # Iva
            float(total),                            # Total
            Decimal('0'),                            # Pagado
            'CREDITO',                               # Referencia
            self._construir_comentario(factura_sat, no_matcheados),  # Comentario
            'PESOS',                                 # Moneda
            Decimal('20.00'),                        # Paridad
            'Credito',                               # Tipo
            proveedor.plazo,                         # Plazo
            float(subtotal),                         # SubTotal2
            factura_sat.folio or '',                  # Factura
            float(total),                            # Saldo (= Total, no pagada)
            settings.usuario_sistema,                # Capturo
            settings.usuario_sistema,                # CapturoCambio
            int(total_articulos),                    # Articulos
            total_partidas,                          # Partidas
            None,                                    # ProcesadaFecha
            1,                                       # IntContable
            'COMPRAS',                               # TipoRecepcion
            0,                                       # Consolidacion = 0 (NO es consolidacion)
            factura_sat.rfc_emisor,                  # RFC
            factura_sat.uuid.upper(),                    # TimbradoFolioFiscal
            factura_sat.fecha_emision,               # FacturaFecha
            settings.sucursal,                       # Sucursal
            'NA',                                    # Departamento
            'TIENDA',                                # Afectacion
            metodo_pago,                             # MetododePago
            0,                                       # NumOC
            total_letra,                             # TotalLetra
            proveedor.ciudad,                        # Ciudad
            proveedor.estado,                        # Estado
            proveedor.tipo,                          # TipoProveedor
            float(total),                            # TotalPrecio
            float(total),                            # TotalRecibidoNeto
            '',                                      # SerieRFC
        )

        cursor.execute(query, params)
        logger.debug(f"Insertada cabecera F-{num_rec}")

    def _construir_comentario(
        self,
        factura_sat: Factura,
        no_matcheados: List[ResultadoMatchProducto],
    ) -> str:
        """Construir comentario para SAVRecC.Comentario.
        Si hay conceptos sin match, los lista con cantidad para que las
        capturistas puedan registrarlos manualmente desde el ERP."""
        comentario = f'CFDI Folio: {factura_sat.folio or "S/N"}'
        if no_matcheados:
            faltantes = []
            for m in no_matcheados:
                c = m.concepto_xml
                cant = int(c.cantidad) if c.cantidad == int(c.cantidad) else float(c.cantidad)
                importe = float(c.importe) if c.importe else 0
                faltantes.append(
                    f'{c.descripcion} ({cant} {c.unidad or "PZ"} ${importe:,.2f})'
                )
            comentario += f' | PARCIAL - Sin match: {"; ".join(faltantes)}'
        return comentario

    def _insertar_detalles(
        self,
        cursor,
        num_rec: int,
        proveedor: ProveedorERP,
        matcheados: List[ResultadoMatchProducto],
    ):
        """Insertar detalles de factura en SAVRecD (Serie F)"""

        query = f"""
            INSERT INTO {self.config.tabla_detalle_recepciones} (
                Serie, NumRec, Producto, Talla, Nombre,
                Proveedor, Cantidad, Costo, CostoImp, PorcDesc,
                PorcIva, NumOC, Unidad, Unidad2, Unidad2Valor,
                Servicio, Registro1, ControlTalla, CodProv, Modelo,
                Pedimento, Orden, ComplementoIva, CantidadNeta, CostoDif,
                Precio, CantidadUM2, Lotes, UltimoCostoC
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
        """

        for secuencia, match in enumerate(matcheados, start=1):
            concepto = match.concepto_xml
            producto = match.producto_erp

            # Determinar PorcIva: usar del XML si tiene, sino del catalogo
            porc_iva = float(producto.porc_iva)
            if concepto.impuesto_iva_tasa is not None:
                tasa_xml = float(concepto.impuesto_iva_tasa * 100)
                if tasa_xml > 0:
                    porc_iva = tasa_xml

            params = (
                SERIE_FACTURA,                       # Serie
                num_rec,                              # NumRec
                producto.codigo,                      # Producto (codigo ERP)
                secuencia,                            # Talla (secuencial)
                producto.nombre[:80],                 # Nombre (del catalogo ERP)
                proveedor.clave,                      # Proveedor
                float(concepto.cantidad),             # Cantidad (del XML)
                float(concepto.valor_unitario),       # Costo (del XML)
                0,                                    # CostoImp (patron manual = 0)
                0,                                    # PorcDesc
                porc_iva,                             # PorcIva
                0,                                    # NumOC
                producto.unidad or 'KG',              # Unidad (del catalogo)
                '',                                   # Unidad2
                1,                                    # Unidad2Valor
                1 if producto.servicio else 0,        # Servicio
                1,                                    # Registro1
                0,                                    # ControlTalla
                '',                                   # CodProv (vacio, patron manual)
                '',                                   # Modelo
                '',                                   # Pedimento
                secuencia,                            # Orden
                0,                                    # ComplementoIva
                float(match.cantidad_neta),           # CantidadNeta (del Google Sheets o 0)
                0,                                    # CostoDif
                0,                                    # Precio
                0,                                    # CantidadUM2
                0,                                    # Lotes
                0,                                    # UltimoCostoC
            )

            cursor.execute(query, params)

        logger.debug(f"Insertados {len(matcheados)} detalles para F-{num_rec}")
