"""
Modulo de registro de Notas de Credito (CFDI Egreso) en SAV7.
Crea registros en SAVNCredP (encabezado), SAVNCredPDet (detalle) y
SAVNCredPRec (vinculacion con factura Serie F).

Las NCs se crean con Estatus='No Aplicada'. La acreditacion
(actualizar NCredito/Saldo en SAVRecC) se hace manualmente despues.
"""
from typing import List, Optional
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from loguru import logger

from config.settings import settings, sav7_config
from src.erp.sav7_connector import SAV7Connector
from src.erp.factura_repo import FacturaRepository
from src.erp.models import (
    ProveedorERP, ResultadoRegistro, FacturaVinculada,
    ConceptoRegistrado, ResultadoMatchProducto
)
from src.erp.utils import numero_a_letra
from src.sat.models import Factura
from src.cfdi.attachment_manager import AttachmentManager


SERIE_NC = 'NCF'

# Claves SAT genericas que indican DESCUENTO (no producto real)
CLAVES_SAT_GENERICAS = {'01010101', '60010100', '84111506'}

# Solo SIGMA puede tener NCs tipo DESCUENTOS.
# Todos los demas proveedores siempre son DEVOLUCIONES.
RFC_PROVEEDORES_CON_DESCUENTO = {'SAC991222G1A'}


class RegistradorNC:
    """
    Registra Notas de Credito (CFDI Egreso) en SAV7.
    Inserta en SAVNCredP, SAVNCredPDet y SAVNCredPRec.
    """

    def __init__(
        self,
        connector: Optional[SAV7Connector] = None,
        factura_repo: Optional[FacturaRepository] = None,
    ):
        self.connector = connector or SAV7Connector()
        self.factura_repo = factura_repo or FacturaRepository(self.connector)
        self.config = sav7_config

    def determinar_tipo_nc(
        self,
        factura_sat: Factura,
        matches: List[ResultadoMatchProducto],
    ) -> str:
        """
        Determinar tipo de NC: DEVOLUCIONES o DESCUENTOS.

        Regla de negocio:
        - Solo SIGMA (RFC_PROVEEDORES_CON_DESCUENTO) puede tener DESCUENTOS
        - Todos los demas proveedores siempre son DEVOLUCIONES
        - Para SIGMA: si claves SAT son genericas -> DESCUENTOS, si no -> DEVOLUCIONES

        Args:
            factura_sat: Factura CFDI Egreso parseada
            matches: Resultados del matching de productos

        Returns:
            'DEVOLUCIONES' o 'DESCUENTOS'
        """
        rfc_emisor = factura_sat.rfc_emisor.upper().strip()

        # Solo proveedores autorizados pueden tener DESCUENTOS
        if rfc_emisor not in RFC_PROVEEDORES_CON_DESCUENTO:
            logger.info(
                f"Tipo NC determinado: DEVOLUCIONES "
                f"(proveedor {rfc_emisor} no tiene descuentos)"
            )
            return 'DEVOLUCIONES'

        # Para proveedores con descuento: verificar claves SAT
        todas_genericas = all(
            c.clave_prod_serv in CLAVES_SAT_GENERICAS
            for c in factura_sat.conceptos
        )
        if todas_genericas:
            logger.info("Tipo NC determinado: DESCUENTOS (claves SAT genericas)")
            return 'DESCUENTOS'

        # Tiene productos reales -> DEVOLUCIONES
        logger.info(
            f"Tipo NC determinado: DEVOLUCIONES "
            f"(proveedor con descuento pero conceptos con productos reales)"
        )
        return 'DEVOLUCIONES'

    def verificar_uuid_nc_existe(self, uuid: str) -> bool:
        """
        Verificar si un UUID ya existe en SAVNCredP para prevenir duplicados.

        Args:
            uuid: UUID del CFDI Egreso a verificar

        Returns:
            True si ya existe, False si no
        """
        query = """
            SELECT COUNT(*) as cuenta
            FROM SAVNCredP
            WHERE TimbradoFolioFiscal = ? AND Serie = ?
        """
        resultados = self.connector.execute_custom_query(
            query, (uuid.upper(), SERIE_NC)
        )
        cuenta = resultados[0]['cuenta'] if resultados else 0
        return cuenta > 0

    def verificar_nc_duplicada(self, factura_sat: 'Factura') -> bool:
        """
        Verificar si una NC ya existe en SAVNCredP por RFC + NCreditoProv + Total.
        Complementa verificar_uuid_nc_existe() cuando TimbradoFolioFiscal va vacio.

        Args:
            factura_sat: Factura CFDI Egreso parseada del XML

        Returns:
            True si ya existe una NC con los mismos datos, False si no
        """
        folio = factura_sat.folio or ''
        if not folio:
            return False  # Sin folio no se puede validar

        query = """
            SELECT COUNT(*) as cuenta
            FROM SAVNCredP
            WHERE Serie = ?
              AND RFC = ?
              AND NCreditoProv = ?
              AND Total = ?
        """
        resultados = self.connector.execute_custom_query(
            query, (
                SERIE_NC,
                factura_sat.rfc_emisor,
                folio,
                float(factura_sat.total),
            )
        )
        cuenta = resultados[0]['cuenta'] if resultados else 0
        return cuenta > 0

    def obtener_siguiente_ncredito(self) -> int:
        """Obtener el siguiente NCredito disponible para Serie NCF (dry-run)"""
        query = """
            SELECT ISNULL(MAX(NCredito), 0) + 1 as SiguienteNum
            FROM SAVNCredP
            WHERE Serie = ?
        """
        resultados = self.connector.execute_custom_query(query, (SERIE_NC,))
        siguiente = resultados[0]['SiguienteNum'] if resultados else 1
        return max(siguiente, settings.ncredito_rango_minimo)

    def registrar(
        self,
        factura_sat: Factura,
        proveedor: ProveedorERP,
        matches: List[ResultadoMatchProducto],
        dry_run: bool = False,
    ) -> ResultadoRegistro:
        """
        Registrar una Nota de Credito CFDI en el ERP.

        Flujo:
        1. Extraer UUID relacionado de cfdi_relacionados[0]
        2. Buscar factura vinculada en SAVRecC por UUID
        3. Validar factura (existe, proveedor coincide, estatus)
        4. Determinar TipoNCredito (DEVOLUCIONES / DESCUENTOS)
        5. Verificar UUID NC no duplicado
        6. Transaccion atomica: INSERT SAVNCredP + SAVNCredPDet + SAVNCredPRec

        Args:
            factura_sat: Factura CFDI Egreso parseada del XML
            proveedor: Proveedor encontrado en SAVProveedor
            matches: Lista de resultados de matching de productos
            dry_run: Si True, simula sin escribir en BD

        Returns:
            ResultadoRegistro con el resultado
        """
        uuid = factura_sat.uuid.upper()
        total_conceptos = len(factura_sat.conceptos)

        # 1. Verificar que tiene CFDI relacionados
        if not factura_sat.cfdi_relacionados:
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                es_nota_credito=True,
                mensaje="NC sin CFDI relacionado: no se puede vincular a factura",
                error="SIN_CFDI_RELACIONADO"
            )

        uuid_factura_relacionada = factura_sat.cfdi_relacionados[0].uuid

        # 2. Buscar factura vinculada en SAVRecC
        factura_vinculada = self.factura_repo.buscar_por_uuid(
            uuid_factura_relacionada
        )
        if not factura_vinculada:
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                es_nota_credito=True,
                factura_vinculada_uuid=uuid_factura_relacionada,
                mensaje=(
                    f"Factura vinculada no encontrada: "
                    f"UUID={uuid_factura_relacionada[:12]}..."
                ),
                error="FACTURA_VINCULADA_NO_ENCONTRADA"
            )

        # 3. Validar factura para NC
        monto_nc = float(factura_sat.total)
        valida, msg_error = self.factura_repo.validar_para_nc(
            factura_vinculada, monto_nc, proveedor.clave
        )
        if not valida:
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                es_nota_credito=True,
                factura_vinculada_uuid=uuid_factura_relacionada,
                factura_vinculada_erp=f"F-{factura_vinculada.num_rec}",
                mensaje=f"Factura no apta para NC: {msg_error}",
                error="FACTURA_NO_APTA"
            )

        # 4. Determinar tipo de NC
        tipo_nc = self.determinar_tipo_nc(factura_sat, matches)

        # 5. Verificar UUID NC no duplicado
        if self.verificar_uuid_nc_existe(uuid):
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                es_nota_credito=True,
                tipo_nc=tipo_nc,
                factura_vinculada_uuid=uuid_factura_relacionada,
                factura_vinculada_erp=f"F-{factura_vinculada.num_rec}",
                mensaje=f"UUID NC ya existe en SAVNCredP: {uuid}",
                error="UUID_NC_DUPLICADO"
            )

        # 5b. Verificar duplicado por RFC + Folio + Total
        # (complementa UUID cuando TimbradoFolioFiscal va vacio)
        if self.verificar_nc_duplicada(factura_sat):
            folio = factura_sat.folio or ''
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                es_nota_credito=True,
                tipo_nc=tipo_nc,
                factura_vinculada_uuid=uuid_factura_relacionada,
                factura_vinculada_erp=f"F-{factura_vinculada.num_rec}",
                mensaje=(
                    f"NC duplicada: RFC={factura_sat.rfc_emisor}, "
                    f"Folio={folio}, Total=${float(factura_sat.total):,.2f}"
                ),
                error="NC_DUPLICADA"
            )

        # Preparar datos de registro segun tipo
        matcheados = [m for m in matches if m.matcheado]
        no_matcheados = [m for m in matches if not m.matcheado]
        conceptos_ok = len(matcheados) if tipo_nc == 'DEVOLUCIONES' else total_conceptos

        if dry_run:
            logger.info(
                f"[DRY-RUN] Se registraria NC {tipo_nc} para UUID {uuid[:12]}... "
                f"vinculada a F-{factura_vinculada.num_rec}"
            )
            return ResultadoRegistro(
                exito=True,
                factura_uuid=uuid,
                numero_factura_erp=f"NCF-DRYRUN",
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                es_nota_credito=True,
                tipo_nc=tipo_nc,
                factura_vinculada_uuid=uuid_factura_relacionada,
                factura_vinculada_erp=f"F-{factura_vinculada.num_rec}",
                conceptos_registrados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        producto_erp=m.producto_erp,
                        registrado=True,
                        confianza_match=m.confianza,
                        metodo_match=m.metodo_match,
                    ) for m in matcheados
                ] if tipo_nc == 'DEVOLUCIONES' else [],
                conceptos_no_matcheados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        motivo_no_registro=m.mensaje
                    ) for m in no_matcheados
                ] if tipo_nc == 'DEVOLUCIONES' else [],
                mensaje=(
                    f"[DRY-RUN] NC {tipo_nc} -> F-{factura_vinculada.num_rec}"
                )
            )

        try:
            # Ejecutar en transaccion unica: SELECT MAX+1 con lock + INSERTs
            # El UPDLOCK/HOLDLOCK previene que otro proceso obtenga el mismo
            # NCredito entre el SELECT y el INSERT (misma proteccion que
            # registro_directo.py para NumRec).
            with self.connector.db.get_cursor() as cursor:
                nuevo_ncredito = self._obtener_siguiente_ncredito_con_lock(cursor)

                logger.info(
                    f"Registrando NC: UUID {uuid[:12]}... -> NCF-{nuevo_ncredito} "
                    f"({tipo_nc}) vinculada a F-{factura_vinculada.num_rec}"
                )

                self._insertar_cabecera_nc(
                    cursor, nuevo_ncredito, factura_sat, proveedor,
                    factura_vinculada, tipo_nc, no_matcheados, matcheados
                )
                self._insertar_detalles_nc(
                    cursor, nuevo_ncredito, factura_sat, proveedor,
                    matcheados, tipo_nc
                )
                self._insertar_vinculacion_nc(
                    cursor, nuevo_ncredito, factura_vinculada
                )

            logger.info(f"Registro NC exitoso: NCF-{nuevo_ncredito}")

            if factura_sat.archivo_xml:
                try:
                    am = AttachmentManager(self.connector)
                    am.adjuntar_nc(
                        xml_origen=Path(factura_sat.archivo_xml),
                        rfc_emisor=factura_sat.rfc_emisor,
                        ncredito=nuevo_ncredito,
                        fecha=factura_sat.fecha_emision,
                    )
                except Exception as e:
                    logger.warning(f"No se pudo adjuntar XML de NCF-{nuevo_ncredito}: {e}")

            return ResultadoRegistro(
                exito=True,
                factura_uuid=uuid,
                numero_factura_erp=f"NCF-{nuevo_ncredito}",
                total_conceptos=total_conceptos,
                conceptos_matcheados_count=conceptos_ok,
                es_nota_credito=True,
                tipo_nc=tipo_nc,
                factura_vinculada_uuid=uuid_factura_relacionada,
                factura_vinculada_erp=f"F-{factura_vinculada.num_rec}",
                conceptos_registrados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        producto_erp=m.producto_erp,
                        registrado=True,
                        confianza_match=m.confianza,
                        metodo_match=m.metodo_match,
                    ) for m in matcheados
                ] if tipo_nc == 'DEVOLUCIONES' else [],
                conceptos_no_matcheados=[
                    ConceptoRegistrado(
                        concepto_xml=m.concepto_xml,
                        motivo_no_registro=m.mensaje
                    ) for m in no_matcheados
                ] if tipo_nc == 'DEVOLUCIONES' else [],
                mensaje=(
                    f"Registrado NCF-{nuevo_ncredito}: {tipo_nc} "
                    f"-> F-{factura_vinculada.num_rec}"
                )
            )

        except Exception as e:
            logger.error(f"Error al registrar NC {uuid}: {e}")
            return ResultadoRegistro(
                exito=False,
                factura_uuid=uuid,
                total_conceptos=total_conceptos,
                es_nota_credito=True,
                tipo_nc=tipo_nc,
                factura_vinculada_uuid=uuid_factura_relacionada,
                factura_vinculada_erp=f"F-{factura_vinculada.num_rec}",
                mensaje=f"Error en registro NC: {str(e)}",
                error=str(e)
            )

    def _obtener_siguiente_ncredito_con_lock(self, cursor) -> int:
        """
        Obtener siguiente NCredito DENTRO de la transaccion actual con lock.

        Usa UPDLOCK + HOLDLOCK para bloquear las filas leidas hasta que
        la transaccion termine (commit o rollback). Esto previene que
        otro proceso obtenga el mismo NCredito.

        Ademas, aplica rango reservado (settings.ncredito_rango_minimo)
        para separar los numeros del Agente 3 del rango del ERP manual.

        IMPORTANTE: Este metodo DEBE ejecutarse dentro de un
        'with self.connector.db.get_cursor() as cursor' que tambien
        contenga los INSERTs posteriores.
        """
        query = """
            SELECT ISNULL(MAX(NCredito), 0) + 1 as SiguienteNum
            FROM SAVNCredP WITH (UPDLOCK, HOLDLOCK)
            WHERE Serie = ?
        """
        cursor.execute(query, (SERIE_NC,))
        row = cursor.fetchone()
        siguiente = row[0] if row else 1
        return max(siguiente, settings.ncredito_rango_minimo)

    def _insertar_cabecera_nc(
        self,
        cursor,
        ncredito_num: int,
        factura_sat: Factura,
        proveedor: ProveedorERP,
        factura_vinculada: FacturaVinculada,
        tipo_nc: str,
        no_matcheados: Optional[List[ResultadoMatchProducto]] = None,
        matcheados: Optional[List[ResultadoMatchProducto]] = None,
    ):
        """Insertar encabezado de NC en SAVNCredP"""

        subtotal = float(factura_sat.subtotal)
        iva = float(factura_sat.iva_trasladado)
        total = float(factura_sat.total)

        # Calcular articulos y partidas
        if tipo_nc == 'DESCUENTOS':
            total_articulos = 1
            total_partidas = 1
        else:
            total_articulos = round(
                sum(float(c.cantidad) for c in factura_sat.conceptos)
            )
            total_partidas = len(factura_sat.conceptos)

        # Total en letra
        total_letra = numero_a_letra(Decimal(str(total)))

        # Concepto: patron de produccion
        # "DEVOLUCION RECOC F-{NumRec} FACT: {Factura} FECHA: {dd/MMM/yyyy}"
        meses = {
            1: 'Ene', 2: 'Feb', 3: 'Mar', 4: 'Abr', 5: 'May', 6: 'Jun',
            7: 'Jul', 8: 'Ago', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dic'
        }
        fecha_fv = factura_vinculada.fecha
        fecha_str = f"{fecha_fv.day:02d}/{meses[fecha_fv.month]}/{fecha_fv.year}"
        concepto = (
            f"DEVOLUCION RECOC F-{factura_vinculada.num_rec} "
            f"FACT: {factura_vinculada.factura} FECHA: {fecha_str}"
        )[:60]  # Truncar a 60 chars (limite de campo)

        # Referencia electronica: RFC_RECNC_NCF{num}_YYYYMMDD
        fecha_hoy = datetime.now().strftime('%Y%m%d')
        nc_electronica = (
            f"{factura_sat.rfc_emisor}_RECNC_NCF{ncredito_num:06d}_{fecha_hoy}"
        )

        fecha_actual = datetime.now()

        query = """
            INSERT INTO SAVNCredP (
                Serie, NCredito, Proveedor, ProveedorNombre, Fecha,
                FacturarA,
                RFC, Concepto, Capturo, PorcIva,
                SubTotal, IVA, Total, Procesado, Estatus,
                FechaAlta, UltimoCambio, TotalLetra,
                Recepcion, RecepcionPago, Impresiones,
                Paridad, Moneda, Articulos, Partidas, PartidasMovInv,
                ProcesadaFecha, Comprador, Referencia,
                TipoNCredito, TotalCostoImp, IntContable,
                CapturoCambio, FechaAltaHora, UltimoCambioHora,
                ProcesadaCapturo, IEPS,
                NCreditoElectronica, NCreditoElectronicaExiste,
                NCreditoElectronicaValida,
                TimbradoFolioFiscal, NCreditoElectronicaEstatus,
                Sucursal, Afectacion, TipoRecepcion,
                TotalAcredita, AsignacionCuentas,
                ExportadoConsolida, ExportadoTemp, Importado,
                RetencionIVA, RetencionISR,
                NCreditoFecha, NCreditoProv,
                Comentario
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?
            )
        """

        params = (
            SERIE_NC,                                    # Serie
            ncredito_num,                                # NCredito
            proveedor.clave,                             # Proveedor
            proveedor.empresa[:60],                      # ProveedorNombre
            factura_sat.fecha_emision,                   # Fecha (del XML)
            self._construir_facturar_a(proveedor),       # FacturarA
            factura_sat.rfc_emisor,                      # RFC
            concepto,                                    # Concepto
            settings.usuario_sistema,                    # Capturo
            16,                                          # PorcIva (default 16%)
            subtotal,                                    # SubTotal
            iva,                                         # IVA
            total,                                       # Total
            0,                                           # Procesado
            'No Aplicada',                               # Estatus
            fecha_actual,                                # FechaAlta
            fecha_actual,                                # UltimoCambio
            total_letra,                                 # TotalLetra
            0,                                           # Recepcion
            0,                                           # RecepcionPago
            0,                                           # Impresiones
            Decimal('20.00'),                            # Paridad
            'PESOS',                                     # Moneda
            total_articulos,                             # Articulos
            total_partidas,                              # Partidas
            total_partidas,                              # PartidasMovInv
            None,                                        # ProcesadaFecha
            settings.usuario_sistema,                    # Comprador
            'CREDITO',                                   # Referencia
            tipo_nc,                                     # TipoNCredito
            0,                                           # TotalCostoImp
            1,                                           # IntContable
            settings.usuario_sistema,                    # CapturoCambio
            fecha_actual,                                # FechaAltaHora
            fecha_actual,                                # UltimoCambioHora
            None,                                        # ProcesadaCapturo
            0,                                           # IEPS
            nc_electronica,                              # NCreditoElectronica
            0,                                           # NCreditoElectronicaExiste (no copiamos XML)
            0,                                           # NCreditoElectronicaValida
            factura_sat.uuid.upper(),                          # TimbradoFolioFiscal
            '',                                          # NCreditoElectronicaEstatus
            settings.sucursal,                           # Sucursal
            'TIENDA',                                    # Afectacion
            'COMPRAS',                                   # TipoRecepcion
            total,                                       # TotalAcredita
            0,                                           # AsignacionCuentas
            0,                                           # ExportadoConsolida
            0,                                           # ExportadoTemp
            0,                                           # Importado
            0,                                           # RetencionIVA
            0,                                           # RetencionISR
            factura_sat.fecha_emision,                   # NCreditoFecha (fecha del CFDI Egreso)
            (factura_sat.folio or '')[:30],               # NCreditoProv (folio del proveedor)
            self._construir_comentario_nc(factura_sat, no_matcheados, matcheados, tipo_nc),  # Comentario
        )

        cursor.execute(query, params)
        logger.debug(f"Insertada cabecera NC: NCF-{ncredito_num}")

    def _insertar_detalles_nc(
        self,
        cursor,
        ncredito_num: int,
        factura_sat: Factura,
        proveedor: ProveedorERP,
        matcheados: List[ResultadoMatchProducto],
        tipo_nc: str,
    ):
        """
        Insertar detalles de NC en SAVNCredPDet.

        Para DESCUENTOS: una sola linea con producto generico INSADM094
        Para DEVOLUCIONES: una linea por cada producto matcheado
        """

        query = """
            INSERT INTO SAVNCredPDet (
                Serie, NCredito, Producto, Talla, Nombre,
                Proveedor, Cantidad, Costo, CostoImp, PorcDesc,
                PorcIva, Unidad, Servicio, ControlTalla,
                PorcDesc2, IEPSPorc, PorcDesc3, PorcDesc4,
                PorcDesc5, PorcDesc6, PorcIvaRetencion,
                PorcISRRetencion, Pedimento, Lotes, Orden
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?
            )
        """

        if tipo_nc == 'DESCUENTOS':
            # Una sola linea con producto generico INSADM094
            subtotal = float(factura_sat.subtotal)
            params = (
                SERIE_NC,                                # Serie
                ncredito_num,                            # NCredito
                settings.producto_descuento,             # Producto (INSADM094)
                '',                                      # Talla
                'CLIENTE EXCELENTE/APOYO AL CLIENTE',    # Nombre
                proveedor.clave,                         # Proveedor
                1,                                       # Cantidad (siempre 1)
                subtotal,                                # Costo (= subtotal de la NC)
                0,                                       # CostoImp
                0,                                       # PorcDesc
                0,                                       # PorcIva (0 para descuentos SIGMA)
                'PZA',                                   # Unidad
                1,                                       # Servicio = True
                0,                                       # ControlTalla
                0,                                       # PorcDesc2
                0,                                       # IEPSPorc
                0,                                       # PorcDesc3
                0,                                       # PorcDesc4
                0,                                       # PorcDesc5
                0,                                       # PorcDesc6
                0,                                       # PorcIvaRetencion
                0,                                       # PorcISRRetencion
                0,                                       # Pedimento
                0,                                       # Lotes
                1,                                       # Orden
            )
            cursor.execute(query, params)
            logger.debug(
                f"Insertado detalle DESCUENTO: INSADM094 x1 = ${subtotal:,.2f}"
            )
        else:
            # DEVOLUCIONES: una linea por producto matcheado
            for secuencia, match in enumerate(matcheados, start=1):
                concepto = match.concepto_xml
                producto = match.producto_erp

                # Determinar PorcIva
                porc_iva = float(producto.porc_iva)
                if concepto.impuesto_iva_tasa is not None:
                    tasa_xml = float(concepto.impuesto_iva_tasa * 100)
                    if tasa_xml > 0:
                        porc_iva = tasa_xml

                params = (
                    SERIE_NC,                            # Serie
                    ncredito_num,                        # NCredito
                    producto.codigo,                     # Producto (codigo ERP)
                    '',                                  # Talla
                    producto.nombre[:80],                # Nombre (del catalogo)
                    proveedor.clave,                     # Proveedor
                    float(concepto.cantidad),            # Cantidad (del XML)
                    float(concepto.valor_unitario),      # Costo (del XML)
                    0,                                   # CostoImp
                    0,                                   # PorcDesc
                    porc_iva,                            # PorcIva
                    producto.unidad or 'KG',             # Unidad (del catalogo)
                    1 if producto.servicio else 0,       # Servicio
                    0,                                   # ControlTalla
                    0,                                   # PorcDesc2
                    0,                                   # IEPSPorc
                    0,                                   # PorcDesc3
                    0,                                   # PorcDesc4
                    0,                                   # PorcDesc5
                    0,                                   # PorcDesc6
                    0,                                   # PorcIvaRetencion
                    0,                                   # PorcISRRetencion
                    0,                                   # Pedimento
                    0,                                   # Lotes
                    secuencia,                           # Orden
                )
                cursor.execute(query, params)

            logger.debug(
                f"Insertados {len(matcheados)} detalles DEVOLUCION "
                f"para NCF-{ncredito_num}"
            )

    def _insertar_vinculacion_nc(
        self,
        cursor,
        ncredito_num: int,
        factura_vinculada: FacturaVinculada,
    ):
        """
        Insertar vinculacion NC <-> Factura en SAVNCredPRec.
        Se crea sin acreditacion (RecAcredito=NULL, RecAcreditoCapturo='').
        """

        query = """
            INSERT INTO SAVNCredPRec (
                Serie, NCredito, RecSerie, RecNumRec, RecUUID,
                RecFecha, RecSubTotal1, RecIva,
                RecIvaRetencion, RecISRRetencion,
                RecTotal, RecPagado, RecNCredito, RecAcredita,
                RecPago, RecAcreditoCapturo
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?
            )
        """

        params = (
            SERIE_NC,                                    # Serie
            ncredito_num,                                # NCredito
            factura_vinculada.serie,                     # RecSerie ('F')
            factura_vinculada.num_rec,                   # RecNumRec
            factura_vinculada.uuid,                      # RecUUID
            factura_vinculada.fecha,                     # RecFecha
            factura_vinculada.subtotal,                  # RecSubTotal1
            factura_vinculada.iva,                       # RecIva
            0,                                           # RecIvaRetencion
            0,                                           # RecISRRetencion
            factura_vinculada.total,                     # RecTotal
            factura_vinculada.pagado,                    # RecPagado
            factura_vinculada.ncredito_acumulado,        # RecNCredito (NCs previas)
            0,                                           # RecAcredita (0 = no acreditado aun)
            0,                                           # RecPago
            '',                                          # RecAcreditoCapturo (vacio)
        )

        cursor.execute(query, params)
        logger.debug(
            f"Insertada vinculacion: NCF-{ncredito_num} -> "
            f"F-{factura_vinculada.num_rec}"
        )

    def _construir_facturar_a(self, proveedor: 'ProveedorERP') -> str:
        """Construir campo FacturarA con direccion del proveedor.

        Formato ERP: {Direccion}\\r{Colonia}{CP}\\r{Ciudad}, {Estado}, {Pais}\\r
        Si no tiene direccion, retorna 'NA'.
        """
        if not proveedor.direccion:
            return 'NA'

        linea1 = proveedor.direccion
        linea2 = f'{proveedor.colonia}{proveedor.cp}'
        linea3 = f'{proveedor.ciudad}, {proveedor.estado}, {proveedor.pais}'

        return f'{linea1}\r{linea2}\r{linea3}\r'

    def _construir_comentario_nc(
        self,
        factura_sat: Factura,
        no_matcheados: Optional[List[ResultadoMatchProducto]] = None,
        matcheados: Optional[List[ResultadoMatchProducto]] = None,
        tipo_nc: str = 'DEVOLUCIONES',
    ) -> str:
        """Construir comentario para SAVNCredP.Comentario.

        Distingue 3 casos:
        - DEVOLUCIONES sin ningun match: pide agregar producto manualmente
        - DEVOLUCIONES parcial: lista conceptos sin match
        - Otros: solo UUID

        NOTA: SAVNCredP.Comentario es varchar(150), se trunca.
        """
        matcheados = matcheados or []
        no_matcheados = no_matcheados or []

        # Caso critico: DEVOLUCIONES sin ningun concepto matcheado
        # El detalle queda vacio, la capturista debe agregar el producto
        if tipo_nc == 'DEVOLUCIONES' and not matcheados and no_matcheados:
            faltantes = []
            for m in no_matcheados:
                c = m.concepto_xml
                cant = int(c.cantidad) if c.cantidad == int(c.cantidad) else float(c.cantidad)
                importe = float(c.importe) if c.importe else 0
                faltantes.append(
                    f'{c.descripcion} ({cant} {c.unidad or "PZ"} ${importe:,.2f})'
                )
            comentario = f'SIN DETALLE - Agregar: {"; ".join(faltantes)}'
            return comentario[:150]

        comentario = f'NC Folio: {factura_sat.folio or "S/N"}'
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
        return comentario[:150]
