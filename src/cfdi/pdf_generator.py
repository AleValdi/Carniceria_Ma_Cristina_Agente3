"""
Generador de PDFs representativos de CFDIs usando satcfdi.

Genera archivos PDF (representacion impresa) a partir de archivos XML
de CFDI (Comprobantes Fiscales Digitales por Internet).

Dependencia opcional: si satcfdi no esta instalado, la generacion se
deshabilita silenciosamente y el flujo continua sin PDF.

Requiere: pip install satcfdi
"""
from pathlib import Path
from loguru import logger

# Intentar importar satcfdi (dependencia opcional)
try:
    from satcfdi.cfdi import CFDI
    from satcfdi import render
    SATCFDI_DISPONIBLE = True
except ImportError:
    SATCFDI_DISPONIBLE = False
    logger.warning("satcfdi no instalado. Generacion de PDF desde XML deshabilitada.")
    logger.warning("Para habilitar: pip install satcfdi")


class PDFGenerator:
    """
    Genera PDFs representativos desde archivos XML CFDI.

    Utiliza la libreria satcfdi para parsear el XML y generar
    una representacion impresa en formato PDF.
    """

    def __init__(self):
        """Inicializa el generador de PDF."""
        if not SATCFDI_DISPONIBLE:
            logger.warning("PDFGenerator inicializado sin satcfdi disponible")

    @property
    def disponible(self) -> bool:
        """Indica si la generacion de PDF esta disponible."""
        return SATCFDI_DISPONIBLE

    def generar_desde_xml(self, xml_path: Path, pdf_destino: Path) -> bool:
        """
        Genera un PDF desde un archivo XML CFDI.

        Args:
            xml_path: Ruta al archivo XML fuente (CFDI 3.3 o 4.0)
            pdf_destino: Ruta donde guardar el PDF generado

        Returns:
            True si se genero exitosamente, False en caso contrario
        """
        if not SATCFDI_DISPONIBLE:
            logger.error("satcfdi no disponible para generar PDF")
            return False

        if not xml_path.exists():
            logger.error(f"XML no encontrado: {xml_path}")
            return False

        try:
            # Cargar CFDI desde XML
            logger.debug(f"Cargando XML: {xml_path.name}")
            cfdi = CFDI.from_file(str(xml_path))

            # Asegurar que el directorio destino existe
            pdf_destino.parent.mkdir(parents=True, exist_ok=True)

            # Generar PDF
            logger.debug(f"Generando PDF: {pdf_destino.name}")
            render.pdf_write(cfdi, str(pdf_destino))

            # Verificar que se creo el archivo
            if pdf_destino.exists():
                size_kb = pdf_destino.stat().st_size / 1024
                logger.info(
                    f"PDF generado exitosamente: {pdf_destino.name} "
                    f"({size_kb:.1f} KB)"
                )
                return True
            else:
                logger.error(f"PDF no se creo: {pdf_destino}")
                return False

        except Exception as e:
            logger.error(f"Error generando PDF desde {xml_path.name}: {e}")
            return False
