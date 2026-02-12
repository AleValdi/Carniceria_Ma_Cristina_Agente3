"""
Modelos de datos para el Agente 3 - Registro Directo
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from src.sat.models import Concepto


@dataclass
class ProductoERP:
    """Producto del catalogo SAVProducto"""
    codigo: str           # SAVProducto.Codigo (PK, ej: "FYV002011")
    nombre: str           # SAVProducto.Nombre (ej: "CEBOLLA BLANCA")
    familia1: str         # Familia1Nombre
    familia2: str         # Familia2Nombre
    unidad: str           # Unidad (ej: "KG", "PZA")
    codigo_sat: str       # CodigoSAT
    porc_iva: Decimal     # PorcIva (0 o 16)
    servicio: bool        # Flag de servicio


@dataclass
class ProveedorERP:
    """Proveedor del catalogo SAVProveedor"""
    clave: str            # SAVProveedor.Clave (6 char PK)
    empresa: str          # Empresa (razon social)
    rfc: str              # RFC
    ciudad: str           # Ciudad
    estado: str           # Estado
    tipo: str             # Tipo (ej: "NACIONAL")
    plazo: int            # Dias de credito


@dataclass
class ResultadoMatchProducto:
    """Resultado de intentar matchear un concepto XML con un producto ERP"""
    concepto_xml: Concepto
    producto_erp: Optional[ProductoERP] = None
    confianza: float = 0.0  # 0.0 a 1.0
    nivel_confianza: str = "NO_MATCH"  # EXACTO, ALTA, MEDIA, NO_MATCH
    metodo_match: str = ""  # exacto, historial, codigo_sat, fuzzy_global
    candidatos_descartados: int = 0
    mensaje: str = ""

    @property
    def matcheado(self) -> bool:
        """Indica si se encontro match"""
        return self.producto_erp is not None


@dataclass
class ConceptoRegistrado:
    """Concepto procesado para registro en ERP"""
    concepto_xml: Concepto
    producto_erp: Optional[ProductoERP] = None
    registrado: bool = False
    confianza_match: float = 0.0
    metodo_match: str = ""
    motivo_no_registro: Optional[str] = None


@dataclass
class ResultadoRegistro:
    """Resultado de registrar una factura en el ERP"""
    exito: bool
    factura_uuid: str
    numero_factura_erp: Optional[str] = None  # "F-XXXXX"
    conceptos_registrados: List[ConceptoRegistrado] = field(default_factory=list)
    conceptos_no_matcheados: List[ConceptoRegistrado] = field(default_factory=list)
    total_conceptos: int = 0
    conceptos_matcheados_count: int = 0
    registro_parcial: bool = False  # True si algunos conceptos no matchearon
    mensaje: str = ""
    error: Optional[str] = None

    @property
    def porcentaje_matcheados(self) -> float:
        """Porcentaje de conceptos que matchearon"""
        if self.total_conceptos == 0:
            return 0.0
        return (self.conceptos_matcheados_count / self.total_conceptos) * 100
