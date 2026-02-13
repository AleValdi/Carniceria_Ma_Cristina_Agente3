"""
Gestor de adjuntos CFDI para el Agente 3.

Copia archivos XML (y opcionalmente PDF) a la carpeta de red compartida
de SAV7, y actualiza los campos de factura electronica en la BD.

Replica el comportamiento del AttachmentManager del Agente 2, adaptado
para ambos flujos: Facturas Ingreso (Serie F) y Notas de Credito (Serie NCF).

Formato de nombres SAV7:
  - Facturas: {RFC}_REC_F{NumRec:06d}_{YYYYMMDD}
  - NCs:      {RFC}_RECNC_NCF{NCredito:06d}_{YYYYMMDD}
"""
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from config.settings import settings
from src.erp.sav7_connector import SAV7Connector
from src.cfdi.pdf_generator import PDFGenerator


class AttachmentManager:
    """Gestiona la copia de XMLs CFDI a la carpeta de adjuntos de SAV7."""

    def __init__(self, connector: SAV7Connector):
        self.connector = connector
        self.directorio = settings.cfdi_adjuntos_dir
        self.pdf_generator = PDFGenerator()

    def adjuntar_factura(
        self,
        xml_origen: Path,
        rfc_emisor: str,
        num_rec: int,
        fecha: datetime,
    ) -> bool:
        """
        Copiar XML de factura a carpeta de red y actualizar SAVRecC.

        Args:
            xml_origen: Ruta al XML fuente (en data/xml_entrada/)
            rfc_emisor: RFC del proveedor emisor
            num_rec: NumRec de la factura Serie F creada
            fecha: Fecha de emision del CFDI

        Returns:
            True si la copia y actualizacion fueron exitosas
        """
        nombre_base = self._generar_nombre_factura(rfc_emisor, num_rec, fecha)
        return self._copiar_y_actualizar(
            xml_origen=xml_origen,
            nombre_base=nombre_base,
            tabla="SAVRecC",
            campo="FacturaElectronica",
            where_clause="Serie = 'F' AND NumRec = ?",
            where_params=[num_rec],
        )

    def adjuntar_nc(
        self,
        xml_origen: Path,
        rfc_emisor: str,
        ncredito: int,
        fecha: datetime,
    ) -> bool:
        """
        Copiar XML de nota de credito a carpeta de red y actualizar SAVNCredP.

        Args:
            xml_origen: Ruta al XML fuente (en data/xml_entrada/)
            rfc_emisor: RFC del proveedor emisor
            ncredito: Numero de la NC Serie NCF creada
            fecha: Fecha de emision del CFDI Egreso

        Returns:
            True si la copia y actualizacion fueron exitosas
        """
        nombre_base = self._generar_nombre_nc(rfc_emisor, ncredito, fecha)
        return self._copiar_y_actualizar(
            xml_origen=xml_origen,
            nombre_base=nombre_base,
            tabla="SAVNCredP",
            campo="NCreditoElectronica",
            where_clause="Serie = 'NCF' AND NCredito = ?",
            where_params=[ncredito],
        )

    # --- Metodos internos ---

    def _generar_nombre_factura(
        self, rfc_emisor: str, num_rec: int, fecha: datetime
    ) -> str:
        """Generar nombre SAV7 para factura: {RFC}_REC_F{NumRec:06d}_{YYYYMMDD}"""
        fecha_str = fecha.strftime('%Y%m%d')
        return f"{rfc_emisor}_REC_F{num_rec:06d}_{fecha_str}"

    def _generar_nombre_nc(
        self, rfc_emisor: str, ncredito: int, fecha: datetime
    ) -> str:
        """Generar nombre SAV7 para NC: {RFC}_RECNC_NCF{NCredito:06d}_{YYYYMMDD}"""
        fecha_str = fecha.strftime('%Y%m%d')
        return f"{rfc_emisor}_RECNC_NCF{ncredito:06d}_{fecha_str}"

    def _copiar_y_actualizar(
        self,
        xml_origen: Path,
        nombre_base: str,
        tabla: str,
        campo: str,
        where_clause: str,
        where_params: list,
    ) -> bool:
        """
        Copiar XML a carpeta de red y actualizar campos de factura electronica.

        Patron no-bloqueante: si falla la copia o la actualizacion,
        se loguea warning pero no lanza excepcion.

        Args:
            xml_origen: Ruta al archivo XML fuente
            nombre_base: Nombre base sin extension (ej: RFC_REC_F068908_20260212)
            tabla: Tabla a actualizar (SAVRecC o SAVNCredP)
            campo: Campo base (FacturaElectronica o NCreditoElectronica)
            where_clause: Clausula WHERE del UPDATE (con ?)
            where_params: Parametros para el WHERE

        Returns:
            True si todo fue exitoso, False si hubo algun error
        """
        if not settings.cfdi_adjuntos_habilitado:
            logger.debug("Adjuntos CFDI deshabilitados (CFDI_ADJUNTOS_HABILITADO=false)")
            return False

        # Verificar que el directorio de destino existe
        if not self.directorio.exists():
            logger.warning(
                f"Carpeta de adjuntos no accesible: {self.directorio}. "
                f"No se copiara el XML."
            )
            return False

        # Verificar que el XML fuente existe
        if not xml_origen.exists():
            logger.warning(f"XML fuente no encontrado: {xml_origen}")
            return False

        # Copiar XML
        destino_xml = self.directorio / f"{nombre_base}.xml"
        try:
            shutil.copy2(str(xml_origen), str(destino_xml))
            logger.info(f"  XML copiado: {destino_xml.name}")
        except Exception as e:
            logger.warning(f"Error al copiar XML a {destino_xml}: {e}")
            return False

        # Generar y copiar PDF (no-bloqueante)
        if settings.cfdi_generar_pdf and self.pdf_generator.disponible:
            destino_pdf = self.directorio / f"{nombre_base}.pdf"
            try:
                if self.pdf_generator.generar_desde_xml(xml_origen, destino_pdf):
                    logger.info(f"  PDF generado: {destino_pdf.name}")
                else:
                    logger.warning(f"No se pudo generar PDF para {nombre_base}")
            except Exception as e:
                logger.warning(f"Error generando PDF para {nombre_base}: {e}")

        # Actualizar campos en BD
        # Campos: {campo}, {campo}Existe, {campo}Valida, {campo}Estatus
        try:
            query = """
                UPDATE {tabla} SET
                    {campo} = ?,
                    {campo}Existe = 1,
                    {campo}Valida = 1,
                    {campo}Estatus = 'Vigente'
                WHERE {where}
            """.format(
                tabla=tabla,
                campo=campo,
                where=where_clause,
            )
            params = [nombre_base] + where_params
            with self.connector.db.get_cursor() as cursor:
                cursor.execute(query, params)
            logger.info(f"  BD actualizada: {campo}={nombre_base}")
        except Exception as e:
            logger.warning(
                f"Error al actualizar {tabla}.{campo}: {e}. "
                f"El XML fue copiado pero la BD no se actualizo."
            )
            return False

        return True
