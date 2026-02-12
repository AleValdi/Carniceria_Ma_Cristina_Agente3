"""
Parser de archivos XML CFDI 4.0 del SAT
Extrae información estructurada de facturas electrónicas mexicanas
"""
import re
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Union, Tuple
from lxml import etree
from loguru import logger

from .models import Factura, Concepto, TipoComprobante, MetodoPago


class CFDIParser:
    """Parser para archivos CFDI 4.0 del SAT"""

    # Namespaces del CFDI 4.0
    NAMESPACES = {
        'cfdi': 'http://www.sat.gob.mx/cfd/4',
        'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital',
        'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
    }

    # Namespace alternativo para CFDI 3.3
    NAMESPACES_33 = {
        'cfdi': 'http://www.sat.gob.mx/cfd/3',
        'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital',
    }

    def __init__(self):
        self.errores: List[str] = []

    def parse_archivo(self, ruta_xml: Union[str, Path]) -> Optional[Factura]:
        """
        Parsear un archivo XML de factura CFDI

        Args:
            ruta_xml: Ruta al archivo XML

        Returns:
            Objeto Factura con los datos extraídos, o None si hay error
        """
        ruta = Path(ruta_xml)

        if not ruta.exists():
            logger.error(f"Archivo no encontrado: {ruta}")
            self.errores.append(f"Archivo no encontrado: {ruta}")
            return None

        try:
            tree = etree.parse(str(ruta))
            root = tree.getroot()

            # Detectar versión del CFDI
            version = root.get('Version') or root.get('version', '')
            if version.startswith('4'):
                ns = self.NAMESPACES
            else:
                ns = self.NAMESPACES_33

            factura = self._parse_comprobante(root, ns)
            if factura:
                factura.archivo_xml = str(ruta)
                factura.fecha_procesamiento = datetime.now()
                logger.info(f"Factura parseada exitosamente: {factura.uuid}")

            return factura

        except etree.XMLSyntaxError as e:
            logger.error(f"Error de sintaxis XML en {ruta}: {e}")
            self.errores.append(f"Error de sintaxis XML: {e}")
            return None
        except Exception as e:
            logger.error(f"Error al parsear {ruta}: {e}")
            self.errores.append(f"Error inesperado: {e}")
            return None

    def parse_string(self, xml_content: str) -> Optional[Factura]:
        """
        Parsear contenido XML como string

        Args:
            xml_content: Contenido XML como string

        Returns:
            Objeto Factura con los datos extraídos
        """
        try:
            root = etree.fromstring(xml_content.encode('utf-8'))

            version = root.get('Version') or root.get('version', '')
            if version.startswith('4'):
                ns = self.NAMESPACES
            else:
                ns = self.NAMESPACES_33

            return self._parse_comprobante(root, ns)

        except Exception as e:
            logger.error(f"Error al parsear XML string: {e}")
            self.errores.append(f"Error al parsear: {e}")
            return None

    def _parse_comprobante(self, root: etree._Element, ns: dict) -> Optional[Factura]:
        """Parsear el nodo principal del comprobante"""
        try:
            # Extraer datos del timbre fiscal
            timbre = root.find('.//tfd:TimbreFiscalDigital', ns)
            if timbre is None:
                # Intentar con namespace alternativo
                timbre = root.find('.//{http://www.sat.gob.mx/TimbreFiscalDigital}TimbreFiscalDigital')

            uuid = timbre.get('UUID') if timbre is not None else None
            fecha_timbrado_str = timbre.get('FechaTimbrado') if timbre is not None else None

            if not uuid:
                logger.warning("No se encontró UUID en el comprobante")
                self.errores.append("UUID no encontrado")
                return None

            # Extraer emisor
            emisor = root.find('cfdi:Emisor', ns)
            if emisor is None:
                emisor = root.find('.//{http://www.sat.gob.mx/cfd/4}Emisor')
                if emisor is None:
                    emisor = root.find('.//{http://www.sat.gob.mx/cfd/3}Emisor')

            # Extraer receptor
            receptor = root.find('cfdi:Receptor', ns)
            if receptor is None:
                receptor = root.find('.//{http://www.sat.gob.mx/cfd/4}Receptor')
                if receptor is None:
                    receptor = root.find('.//{http://www.sat.gob.mx/cfd/3}Receptor')

            # Crear factura
            factura = Factura(
                uuid=uuid,
                serie=root.get('Serie'),
                folio=root.get('Folio'),
                fecha_emision=self._parse_fecha(root.get('Fecha')),
                fecha_timbrado=self._parse_fecha(fecha_timbrado_str),
                rfc_emisor=emisor.get('Rfc', '') if emisor is not None else '',
                nombre_emisor=emisor.get('Nombre', '') if emisor is not None else '',
                regimen_fiscal_emisor=emisor.get('RegimenFiscal', '') if emisor is not None else '',
                rfc_receptor=receptor.get('Rfc', '') if receptor is not None else '',
                nombre_receptor=receptor.get('Nombre', '') if receptor is not None else '',
                uso_cfdi=receptor.get('UsoCFDI', '') if receptor is not None else '',
                domicilio_fiscal_receptor=receptor.get('DomicilioFiscalReceptor') if receptor is not None else None,
                regimen_fiscal_receptor=receptor.get('RegimenFiscalReceptor') if receptor is not None else None,
                tipo_comprobante=self._parse_tipo_comprobante(root.get('TipoDeComprobante')),
                metodo_pago=self._parse_metodo_pago(root.get('MetodoPago')),
                forma_pago=root.get('FormaPago'),
                condiciones_pago=root.get('CondicionesDePago'),
                subtotal=self._parse_decimal(root.get('SubTotal', '0')),
                descuento=self._parse_decimal(root.get('Descuento', '0')),
                total=self._parse_decimal(root.get('Total', '0')),
                moneda=root.get('Moneda', 'MXN'),
                tipo_cambio=self._parse_decimal(root.get('TipoCambio', '1')),
            )

            # Parsear impuestos
            self._parse_impuestos(root, factura, ns)

            # Parsear conceptos
            factura.conceptos = self._parse_conceptos(root, ns)

            # Extraer número de remisión si está indicado en la factura
            factura.numero_remision_indicado = self._extraer_numero_remision(factura)

            return factura

        except Exception as e:
            logger.error(f"Error al parsear comprobante: {e}")
            self.errores.append(f"Error en comprobante: {e}")
            return None

    def _parse_conceptos(self, root: etree._Element, ns: dict) -> List[Concepto]:
        """Parsear todos los conceptos de la factura"""
        conceptos = []

        # Buscar nodo Conceptos
        conceptos_node = root.find('cfdi:Conceptos', ns)
        if conceptos_node is None:
            conceptos_node = root.find('.//{http://www.sat.gob.mx/cfd/4}Conceptos')
            if conceptos_node is None:
                conceptos_node = root.find('.//{http://www.sat.gob.mx/cfd/3}Conceptos')

        if conceptos_node is None:
            return conceptos

        for concepto_elem in conceptos_node:
            if 'Concepto' in concepto_elem.tag:
                try:
                    concepto = Concepto(
                        clave_prod_serv=concepto_elem.get('ClaveProdServ', ''),
                        descripcion=concepto_elem.get('Descripcion', ''),
                        cantidad=self._parse_decimal(concepto_elem.get('Cantidad', '1')),
                        clave_unidad=concepto_elem.get('ClaveUnidad', ''),
                        unidad=concepto_elem.get('Unidad'),
                        valor_unitario=self._parse_decimal(concepto_elem.get('ValorUnitario', '0')),
                        importe=self._parse_decimal(concepto_elem.get('Importe', '0')),
                        descuento=self._parse_decimal(concepto_elem.get('Descuento', '0')),
                        objeto_imp=concepto_elem.get('ObjetoImp'),
                    )

                    # Parsear impuestos del concepto
                    self._parse_impuestos_concepto(concepto_elem, concepto, ns)

                    conceptos.append(concepto)

                except Exception as e:
                    logger.warning(f"Error al parsear concepto: {e}")

        return conceptos

    def _parse_impuestos(self, root: etree._Element, factura: Factura, ns: dict):
        """Parsear impuestos totales del comprobante"""
        impuestos = root.find('cfdi:Impuestos', ns)
        if impuestos is None:
            impuestos = root.find('.//{http://www.sat.gob.mx/cfd/4}Impuestos')
            if impuestos is None:
                impuestos = root.find('.//{http://www.sat.gob.mx/cfd/3}Impuestos')

        if impuestos is None:
            return

        # Totales de impuestos
        factura.total_impuestos_trasladados = self._parse_decimal(
            impuestos.get('TotalImpuestosTrasladados', '0')
        )
        factura.total_impuestos_retenidos = self._parse_decimal(
            impuestos.get('TotalImpuestosRetenidos', '0')
        )

        # Buscar IVA trasladado
        traslados = impuestos.find('cfdi:Traslados', ns)
        if traslados is None:
            traslados = impuestos.find('.//{http://www.sat.gob.mx/cfd/4}Traslados')
            if traslados is None:
                traslados = impuestos.find('.//{http://www.sat.gob.mx/cfd/3}Traslados')

        if traslados is not None:
            for traslado in traslados:
                if 'Traslado' in traslado.tag:
                    impuesto = traslado.get('Impuesto', '')
                    if impuesto == '002':  # IVA
                        factura.iva_trasladado += self._parse_decimal(traslado.get('Importe', '0'))

        # Buscar retenciones
        retenciones = impuestos.find('cfdi:Retenciones', ns)
        if retenciones is None:
            retenciones = impuestos.find('.//{http://www.sat.gob.mx/cfd/4}Retenciones')
            if retenciones is None:
                retenciones = impuestos.find('.//{http://www.sat.gob.mx/cfd/3}Retenciones')

        if retenciones is not None:
            for retencion in retenciones:
                if 'Retencion' in retencion.tag:
                    impuesto = retencion.get('Impuesto', '')
                    importe = self._parse_decimal(retencion.get('Importe', '0'))
                    if impuesto == '001':  # ISR
                        factura.isr_retenido += importe
                    elif impuesto == '002':  # IVA
                        factura.iva_retenido += importe

    def _parse_impuestos_concepto(self, concepto_elem: etree._Element, concepto: Concepto, ns: dict):
        """Parsear impuestos de un concepto individual"""
        impuestos = concepto_elem.find('cfdi:Impuestos', ns)
        if impuestos is None:
            for child in concepto_elem:
                if 'Impuestos' in child.tag:
                    impuestos = child
                    break

        if impuestos is None:
            return

        # Traslados
        traslados = impuestos.find('cfdi:Traslados', ns)
        if traslados is None:
            for child in impuestos:
                if 'Traslados' in child.tag:
                    traslados = child
                    break

        if traslados is not None:
            for traslado in traslados:
                if 'Traslado' in traslado.tag:
                    impuesto = traslado.get('Impuesto', '')
                    if impuesto == '002':  # IVA
                        concepto.impuesto_iva_tasa = self._parse_decimal(traslado.get('TasaOCuota', '0'))
                        concepto.impuesto_iva_importe = self._parse_decimal(traslado.get('Importe', '0'))

    def _parse_fecha(self, fecha_str: Optional[str]) -> Optional[datetime]:
        """Parsear fecha en formato ISO"""
        if not fecha_str:
            return None
        try:
            return datetime.fromisoformat(fecha_str.replace('Z', '+00:00'))
        except ValueError:
            try:
                return datetime.strptime(fecha_str, '%Y-%m-%dT%H:%M:%S')
            except ValueError:
                return None

    def _parse_decimal(self, value: Optional[str]) -> Decimal:
        """Parsear valor decimal de forma segura"""
        if not value:
            return Decimal("0")
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return Decimal("0")

    def _parse_tipo_comprobante(self, tipo: Optional[str]) -> TipoComprobante:
        """Parsear tipo de comprobante"""
        if not tipo:
            return TipoComprobante.INGRESO
        try:
            return TipoComprobante(tipo)
        except ValueError:
            return TipoComprobante.INGRESO

    def _parse_metodo_pago(self, metodo: Optional[str]) -> Optional[MetodoPago]:
        """Parsear método de pago"""
        if not metodo:
            return None
        try:
            return MetodoPago(metodo)
        except ValueError:
            return None

    def _extraer_numero_remision(self, factura: Factura) -> Optional[str]:
        """
        Extraer número de remisión indicado en la factura.
        Busca en: CondicionesDePago, descripciones de conceptos

        Patrones buscados:
        - REM-123, REM123, REM 123
        - REMISION 123, REMISION: 123
        - RECEPCION 123, RECEPCION: 123
        - REC-123, REC123
        - Serie-Numero (ej: A-12345)

        Returns:
            Número de remisión encontrado o None
        """
        # Patrones para buscar números de remisión
        patrones = [
            # REM-123, REM123, REM 123, REM:123
            r'REM[\s\-:]*(\d+)',
            # REMISION 123, REMISION: 123, REMISION #123
            r'REMISI[OÓ]N[\s\-:#]*(\d+)',
            # RECEPCION 123, RECEPCION: 123
            r'RECEPCI[OÓ]N[\s\-:#]*(\d+)',
            # REC-123, REC123
            r'REC[\s\-:]*(\d+)',
            # Folio: 123, FOLIO 123
            r'FOLIO[\s\-:#]*(\d+)',
            # Serie-Numero con letra y guion (A-12345, R-789)
            r'\b([A-Z][\-]\d{3,})\b',
        ]

        textos_a_buscar = []

        # 1. Buscar en CondicionesDePago
        if factura.condiciones_pago:
            textos_a_buscar.append(factura.condiciones_pago)

        # 2. Buscar en descripciones de conceptos
        for concepto in factura.conceptos:
            if concepto.descripcion:
                textos_a_buscar.append(concepto.descripcion)

        # Buscar en todos los textos
        for texto in textos_a_buscar:
            texto_upper = texto.upper()
            for patron in patrones:
                match = re.search(patron, texto_upper, re.IGNORECASE)
                if match:
                    numero = match.group(1)
                    logger.info(f"Número de remisión encontrado en factura: {numero}")
                    return numero

        return None

    def parse_directorio(self, directorio: Union[str, Path]) -> List[Factura]:
        """
        Parsear todos los archivos XML en un directorio

        Args:
            directorio: Ruta al directorio con archivos XML

        Returns:
            Lista de facturas parseadas exitosamente
        """
        directorio = Path(directorio)
        facturas = []

        if not directorio.exists():
            logger.error(f"Directorio no encontrado: {directorio}")
            return facturas

        # Usar solo *.xml ya que glob en Windows es case-insensitive
        archivos_xml = list(directorio.glob("*.xml"))

        logger.info(f"Encontrados {len(archivos_xml)} archivos XML en {directorio}")

        for archivo in archivos_xml:
            factura = self.parse_archivo(archivo)
            if factura:
                facturas.append(factura)

        logger.info(f"Parseadas exitosamente {len(facturas)} de {len(archivos_xml)} facturas")

        return facturas
