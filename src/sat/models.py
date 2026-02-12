"""
Modelos de datos para facturas CFDI del SAT
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from enum import Enum


class TipoComprobante(Enum):
    """Tipos de comprobante CFDI"""
    INGRESO = "I"
    EGRESO = "E"
    TRASLADO = "T"
    NOMINA = "N"
    PAGO = "P"


class MetodoPago(Enum):
    """Métodos de pago CFDI"""
    PUE = "PUE"  # Pago en Una sola Exhibición
    PPD = "PPD"  # Pago en Parcialidades o Diferido


@dataclass
class Concepto:
    """Representa un concepto/producto en la factura"""

    clave_prod_serv: str  # Clave del catálogo SAT
    descripcion: str
    cantidad: Decimal
    clave_unidad: str  # Clave de unidad SAT
    unidad: Optional[str]  # Descripción de la unidad
    valor_unitario: Decimal
    importe: Decimal
    descuento: Decimal = Decimal("0")
    objeto_imp: Optional[str] = None  # Objeto de impuesto

    # Impuestos trasladados del concepto
    impuesto_iva_tasa: Optional[Decimal] = None
    impuesto_iva_importe: Optional[Decimal] = None

    # Impuestos retenidos del concepto
    retencion_isr: Optional[Decimal] = None
    retencion_iva: Optional[Decimal] = None

    @property
    def importe_neto(self) -> Decimal:
        """Importe menos descuento"""
        return self.importe - self.descuento

    def to_dict(self) -> dict:
        """Convertir a diccionario"""
        return {
            'clave_prod_serv': self.clave_prod_serv,
            'descripcion': self.descripcion,
            'cantidad': float(self.cantidad),
            'clave_unidad': self.clave_unidad,
            'unidad': self.unidad,
            'valor_unitario': float(self.valor_unitario),
            'importe': float(self.importe),
            'descuento': float(self.descuento),
            'importe_neto': float(self.importe_neto),
            'iva_tasa': float(self.impuesto_iva_tasa) if self.impuesto_iva_tasa else None,
            'iva_importe': float(self.impuesto_iva_importe) if self.impuesto_iva_importe else None,
        }


@dataclass
class Factura:
    """Representa una factura CFDI completa"""

    # Identificadores
    uuid: str  # UUID del timbre fiscal
    serie: Optional[str]
    folio: Optional[str]

    # Fechas
    fecha_emision: datetime
    fecha_timbrado: datetime

    # Emisor
    rfc_emisor: str
    nombre_emisor: str
    regimen_fiscal_emisor: str

    # Receptor
    rfc_receptor: str
    nombre_receptor: str
    uso_cfdi: str
    domicilio_fiscal_receptor: Optional[str] = None
    regimen_fiscal_receptor: Optional[str] = None

    # Tipo de comprobante
    tipo_comprobante: TipoComprobante = TipoComprobante.INGRESO
    metodo_pago: Optional[MetodoPago] = None
    forma_pago: Optional[str] = None
    condiciones_pago: Optional[str] = None

    # Montos
    subtotal: Decimal = Decimal("0")
    descuento: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    moneda: str = "MXN"
    tipo_cambio: Decimal = Decimal("1")

    # Impuestos totales
    total_impuestos_trasladados: Decimal = Decimal("0")
    total_impuestos_retenidos: Decimal = Decimal("0")
    iva_trasladado: Decimal = Decimal("0")
    isr_retenido: Decimal = Decimal("0")
    iva_retenido: Decimal = Decimal("0")

    # Conceptos
    conceptos: List[Concepto] = field(default_factory=list)

    # Archivo fuente
    archivo_xml: Optional[str] = None

    # Metadatos de procesamiento
    fecha_procesamiento: Optional[datetime] = None
    estatus_procesamiento: str = "pendiente"

    # Referencia a remisión (extraída del XML si está indicada)
    numero_remision_indicado: Optional[str] = None

    @property
    def identificador(self) -> str:
        """Identificador legible de la factura"""
        if self.serie and self.folio:
            return f"{self.serie}-{self.folio}"
        elif self.folio:
            return self.folio
        return self.uuid[:8]

    @property
    def total_conceptos(self) -> int:
        """Número de conceptos en la factura"""
        return len(self.conceptos)

    @property
    def suma_cantidades(self) -> Decimal:
        """Suma total de cantidades de todos los conceptos"""
        return sum(c.cantidad for c in self.conceptos)

    def to_dict(self) -> dict:
        """Convertir a diccionario para reportes"""
        return {
            'uuid': self.uuid,
            'serie': self.serie,
            'folio': self.folio,
            'identificador': self.identificador,
            'fecha_emision': self.fecha_emision.isoformat() if self.fecha_emision else None,
            'fecha_timbrado': self.fecha_timbrado.isoformat() if self.fecha_timbrado else None,
            'rfc_emisor': self.rfc_emisor,
            'nombre_emisor': self.nombre_emisor,
            'rfc_receptor': self.rfc_receptor,
            'nombre_receptor': self.nombre_receptor,
            'tipo_comprobante': self.tipo_comprobante.value if self.tipo_comprobante else None,
            'metodo_pago': self.metodo_pago.value if self.metodo_pago else None,
            'forma_pago': self.forma_pago,
            'subtotal': float(self.subtotal),
            'descuento': float(self.descuento),
            'iva_trasladado': float(self.iva_trasladado),
            'total': float(self.total),
            'moneda': self.moneda,
            'total_conceptos': self.total_conceptos,
            'archivo_xml': self.archivo_xml,
            'conceptos': [c.to_dict() for c in self.conceptos],
        }

    def __str__(self) -> str:
        return f"Factura {self.identificador} - {self.nombre_emisor} - ${self.total:,.2f}"
