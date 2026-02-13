"""
Configuracion del Agente 3 - Registro Directo de Facturas CFDI
"""
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv


def get_base_dir() -> Path:
    """
    Obtener directorio base del proyecto.
    Detecta si esta corriendo como ejecutable PyInstaller o como script Python.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).resolve().parent.parent


# Directorio base del proyecto
BASE_DIR = get_base_dir()

# Cargar variables de entorno desde el directorio base
load_dotenv(BASE_DIR / '.env')

# Si existe .env.local, sobreescribir con config local (desarrollo remoto)
_env_local = BASE_DIR / '.env.local'
if _env_local.exists():
    load_dotenv(_env_local, override=True)


@dataclass
class Settings:
    """Configuracion principal del Agente 3"""

    # Rutas de datos
    input_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "xml_entrada")
    output_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "reportes")
    processed_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "xml_procesados")
    parciales_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "xml_parciales")
    fallidos_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "xml_fallidos")
    logs_dir: Path = field(default_factory=lambda: BASE_DIR / "logs")

    # Configuracion de matching de productos
    umbral_match_producto: int = 90  # Score minimo fuzzy para aceptar match
    umbral_match_exacto: int = 95  # Score minimo para match en catalogo completo (sin historial)
    min_conceptos_match_porcentaje: int = 50  # % minimo de conceptos que deben matchear
    registrar_parciales: bool = True  # Registrar facturas con match parcial

    # Configuracion de registro
    estatus_registro: str = "No Pagada"  # Estatus para nuevos registros Serie F
    usuario_sistema: str = "AGENTE3_SAT"  # Usuario del sistema para campo Comprador
    sucursal: int = 5  # Sucursal por defecto

    # Configuracion de Notas de Credito
    producto_descuento: str = "INSADM094"  # Producto generico para NCs tipo DESCUENTOS

    # Configuracion de reportes
    nombre_reporte: str = "registro_directo_cfdi"
    incluir_fecha_en_reporte: bool = True

    # Configuracion de matching avanzado (paso 2.5: token_set + historial)
    habilitar_token_set_historial: bool = True  # Toggle para paso 2.5
    min_longitud_token_set: int = 5  # Min chars para activar token_set_ratio

    # Validacion cruzada de remisiones pendientes
    validar_remisiones_pendientes: bool = True  # Toggle para activar/desactivar
    tolerancia_monto_validacion: float = 2.0    # % tolerancia para comparar montos
    dias_rango_validacion: int = 15             # +/- dias para comparar fechas

    # Adjuntos CFDI (copia de XML a carpeta de red SAV7)
    cfdi_adjuntos_dir: Path = field(
        default_factory=lambda: Path(os.getenv(
            'CFDI_ADJUNTOS_DIR',
            r'\\SERVERMC\Asesoft\SAV7-1\Recepciones CFDI'
        ))
    )
    cfdi_adjuntos_habilitado: bool = field(
        default_factory=lambda: os.getenv(
            'CFDI_ADJUNTOS_HABILITADO', 'true'
        ).lower() == 'true'
    )
    cfdi_generar_pdf: bool = field(
        default_factory=lambda: os.getenv(
            'CFDI_GENERAR_PDF', 'true'
        ).lower() == 'true'
    )

    # Configuracion de logging
    log_level: str = "INFO"
    log_format: str = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"

    def __post_init__(self):
        """Crear directorios si no existen"""
        for dir_path in [
            self.input_dir, self.output_dir, self.processed_dir,
            self.parciales_dir, self.fallidos_dir, self.logs_dir
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> 'Settings':
        """Crear configuracion desde variables de entorno"""
        return cls(
            umbral_match_producto=int(os.getenv('UMBRAL_MATCH_PRODUCTO', '90')),
            umbral_match_exacto=int(os.getenv('UMBRAL_MATCH_EXACTO', '95')),
            min_conceptos_match_porcentaje=int(os.getenv('MIN_CONCEPTOS_MATCH_PORCENTAJE', '50')),
            registrar_parciales=os.getenv('REGISTRAR_PARCIALES', 'true').lower() == 'true',
            estatus_registro=os.getenv('ESTATUS_REGISTRO', 'No Pagada'),
            usuario_sistema=os.getenv('USUARIO_SISTEMA', 'AGENTE3_SAT'),
            sucursal=int(os.getenv('SUCURSAL', '5')),
            log_level=os.getenv('LOG_LEVEL', 'INFO'),
            habilitar_token_set_historial=os.getenv('HABILITAR_TOKEN_SET_HISTORIAL', 'true').lower() == 'true',
            min_longitud_token_set=int(os.getenv('MIN_LONGITUD_TOKEN_SET', '5')),
            producto_descuento=os.getenv('PRODUCTO_DESCUENTO', 'INSADM094'),
            validar_remisiones_pendientes=os.getenv('VALIDAR_REMISIONES_PENDIENTES', 'true').lower() == 'true',
            tolerancia_monto_validacion=float(os.getenv('TOLERANCIA_MONTO_VALIDACION', '2.0')),
            dias_rango_validacion=int(os.getenv('DIAS_RANGO_VALIDACION', '15')),
        )


@dataclass
class SAV7Config:
    """Configuracion especifica para consultas en SAV7"""

    # Nombres de tablas SAV7
    tabla_recepciones: str = "SAVRecC"
    tabla_detalle_recepciones: str = "SAVRecD"
    tabla_proveedores: str = "SAVProveedor"
    tabla_productos: str = "SAVProducto"

    @classmethod
    def from_env(cls) -> 'SAV7Config':
        """Crear configuracion desde variables de entorno"""
        return cls(
            tabla_recepciones=os.getenv('SAV7_TABLA_RECEPCIONES', 'SAVRecC'),
            tabla_detalle_recepciones=os.getenv('SAV7_TABLA_DETALLE', 'SAVRecD'),
            tabla_proveedores=os.getenv('SAV7_TABLA_PROVEEDORES', 'SAVProveedor'),
            tabla_productos=os.getenv('SAV7_TABLA_PRODUCTOS', 'SAVProducto'),
        )


# Instancias globales de configuracion
settings = Settings.from_env()
sav7_config = SAV7Config.from_env()
