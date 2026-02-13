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
    frecuencia: int = 0   # Veces comprado a un proveedor (para desambiguacion)


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
class FacturaVinculada:
    """Factura Serie F encontrada en SAVRecC para vincular con Nota de Credito"""
    serie: str              # 'F'
    num_rec: int            # NumRec
    factura: str            # Campo Factura (folio proveedor)
    fecha: datetime         # Fecha
    total: float            # Total
    saldo: float            # Saldo
    ncredito_acumulado: float  # NCredito actual acumulado
    pagado: float           # Pagado
    estatus: str            # Estatus ('No Pagada', etc.)
    uuid: str               # TimbradoFolioFiscal
    proveedor: str          # Clave proveedor
    subtotal: float         # SubTotal1
    iva: float              # Iva


@dataclass
class RemisionPendiente:
    """Remision Serie R pendiente encontrada en SAVRecC"""
    serie: str              # 'R'
    num_rec: int            # NumRec
    fecha: datetime         # Fecha de la remision
    total: float            # Total de la remision
    estatus: str            # 'No Pagada', 'RECIBIDA', etc.
    factura: str            # Campo Factura (folio del proveedor)
    proveedor: str          # Clave proveedor
    diferencia_monto_pct: float = 0.0  # % diferencia vs factura CFDI
    diferencia_dias: int = 0           # Dias de diferencia vs factura CFDI


@dataclass
class ResultadoValidacion:
    """Resultado de validacion cruzada contra remisiones pendientes"""
    clasificacion: str  # 'SEGURO', 'REVISAR', 'BLOQUEAR'
    total_remisiones_pendientes: int = 0
    remision_similar: Optional[RemisionPendiente] = None  # Mejor candidato si BLOQUEAR
    remisiones_pendientes: List[RemisionPendiente] = field(default_factory=list)
    mensaje: str = ""


@dataclass
class ResultadoRegistro:
    """Resultado de registrar una factura o nota de credito en el ERP"""
    exito: bool
    factura_uuid: str
    numero_factura_erp: Optional[str] = None  # "F-XXXXX" o "NCF-XXXX"
    conceptos_registrados: List[ConceptoRegistrado] = field(default_factory=list)
    conceptos_no_matcheados: List[ConceptoRegistrado] = field(default_factory=list)
    total_conceptos: int = 0
    conceptos_matcheados_count: int = 0
    registro_parcial: bool = False  # True si algunos conceptos no matchearon
    mensaje: str = ""
    error: Optional[str] = None

    # Campos para Notas de Credito (opcionales, backward compatible)
    es_nota_credito: bool = False
    tipo_nc: str = ""                          # DEVOLUCIONES / DESCUENTOS
    factura_vinculada_uuid: Optional[str] = None  # UUID de la factura F original
    factura_vinculada_erp: Optional[str] = None   # "F-XXXXX" de la factura vinculada

    # Campo para validacion cruzada (backward compatible)
    advertencia_validacion: Optional[str] = None

    @property
    def porcentaje_matcheados(self) -> float:
        """Porcentaje de conceptos que matchearon"""
        if self.total_conceptos == 0:
            return 0.0
        return (self.conceptos_matcheados_count / self.total_conceptos) * 100
