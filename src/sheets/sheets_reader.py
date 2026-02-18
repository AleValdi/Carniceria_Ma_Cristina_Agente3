"""
Lector de Google Sheets para extraer datos de recepcion de mercado.
Lee las columnas DIA, PRODUCTO y CANTIDAD NETA del sheet configurado.

Soporta dos tipos de autenticacion:
  - Service Account (recomendado): solo necesita el JSON de la service account
  - OAuth2 (alternativa): necesita credenciales OAuth2 + token.json
"""
import json
import os
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
from typing import Dict, List, Tuple, Optional
from pathlib import Path

from loguru import logger

try:
    import gspread
    GSPREAD_DISPONIBLE = True
except ImportError:
    GSPREAD_DISPONIBLE = False


class SheetsReader:
    """
    Lee datos de recepcion de mercado desde Google Sheets.

    Extrae las columnas DIA, PRODUCTO y CANTIDAD NETA, y construye
    un indice en memoria indexado por (fecha, producto_normalizado).
    """

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        sheet_id: str,
        hoja_id: int = 0,
    ):
        """
        Args:
            credentials_path: Ruta al archivo de credenciales JSON
                              (Service Account o OAuth2 Client)
            token_path: Ruta donde se guardara/leera el token.json
                        (solo usado con OAuth2, ignorado con Service Account)
            sheet_id: ID del Google Sheet
            hoja_id: Indice de la hoja dentro del sheet (0 = primera)
        """
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.sheet_id = sheet_id
        self.hoja_id = hoja_id

    @property
    def disponible(self) -> bool:
        """Indica si gspread esta instalado"""
        return GSPREAD_DISPONIBLE

    def _detectar_tipo_credenciales(self) -> str:
        """Detectar si las credenciales son Service Account o OAuth2."""
        try:
            with open(self.credentials_path, 'r') as f:
                data = json.load(f)
            if data.get('type') == 'service_account':
                return 'service_account'
            elif 'installed' in data or 'web' in data:
                return 'oauth2'
            else:
                return 'desconocido'
        except Exception:
            return 'desconocido'

    def _autorizar_service_account(self) -> 'gspread.Client':
        """Autorizar con Service Account (sin navegador)."""
        logger.debug("Autenticando con Service Account...")
        return gspread.service_account(filename=self.credentials_path)

    def _autorizar_oauth2(self) -> 'gspread.Client':
        """Autorizar con OAuth2 (requiere navegador la primera vez)."""
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request

        SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        creds = None

        # Cargar token existente
        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            except Exception as e:
                logger.warning(f"Error al cargar token existente: {e}")

        # Si no hay credenciales validas, obtener nuevas
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.warning(f"Error al renovar token: {e}")
                    creds = None

            if not creds:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=8080)

            # Guardar token
            try:
                token_dir = Path(self.token_path).parent
                token_dir.mkdir(parents=True, exist_ok=True)
                with open(self.token_path, 'w') as f:
                    f.write(creds.to_json())
            except Exception as e:
                logger.warning(f"No se pudo guardar token: {e}")

        return gspread.authorize(creds)

    def leer_datos_recepcion(self) -> Dict[Tuple[str, str], Decimal]:
        """
        Lee columnas DIA, PRODUCTO y CANTIDAD NETA del Google Sheet.

        Returns:
            Diccionario indexado por (fecha_str "YYYY-MM-DD", producto_normalizado_upper)
            con el valor de CANTIDAD NETA como Decimal.
            Si hay duplicados (mismo producto, mismo dia), se SUMAN las cantidades.
            Retorna diccionario vacio si hay error.
        """
        if not GSPREAD_DISPONIBLE:
            logger.warning("Google Sheets no disponible (gspread no instalado)")
            return {}

        if not os.path.exists(self.credentials_path):
            logger.error(f"Archivo de credenciales no encontrado: {self.credentials_path}")
            return {}

        try:
            # Detectar tipo de credenciales y autorizar
            tipo = self._detectar_tipo_credenciales()
            if tipo == 'service_account':
                gc = self._autorizar_service_account()
                logger.info("Google Sheets: autenticado con Service Account")
            elif tipo == 'oauth2':
                gc = self._autorizar_oauth2()
                logger.info("Google Sheets: autenticado con OAuth2")
            else:
                logger.error(f"Tipo de credenciales no reconocido en {self.credentials_path}")
                return {}

            sh = gc.open_by_key(self.sheet_id)
            hoja = sh.get_worksheet(self.hoja_id)

            if not hoja:
                logger.error(f"Hoja con indice {self.hoja_id} no encontrada en el sheet")
                return {}

            # Leer datos crudos (get_all_values evita problemas con headers duplicados/vacios)
            todas_las_filas = hoja.get_all_values()
            if len(todas_las_filas) < 2:
                logger.warning("Google Sheets: hoja vacia o sin datos")
                return {}

            headers = todas_las_filas[0]
            registros = []
            for fila in todas_las_filas[1:]:
                registro = {}
                for i, header in enumerate(headers):
                    if header and i < len(fila):
                        registro[header] = fila[i]
                registros.append(registro)

            logger.info(f"Google Sheets: {len(registros)} filas leidas de '{hoja.title}'")

            return self._construir_indice(registros)

        except Exception as e:
            logger.error(f"Error al leer Google Sheets: {e}")
            return {}

    def _construir_indice(
        self,
        registros: List[Dict],
    ) -> Dict[Tuple[str, str], Decimal]:
        """
        Construir indice (fecha, producto) -> cantidad_neta desde los registros.

        Args:
            registros: Lista de dicts con claves del encabezado del sheet

        Returns:
            Dict indexado por (fecha_str, producto_norm) -> Decimal
        """
        from src.matching.cache_productos import normalizar_texto

        indice: Dict[Tuple[str, str], Decimal] = {}
        filas_procesadas = 0
        filas_ignoradas = 0

        for fila in registros:
            # Buscar columnas por nombre (flexible con variantes)
            dia_raw = self._obtener_valor_columna(fila, ['DIA', 'Dia', 'dia', 'FECHA', 'Fecha'])
            producto_raw = self._obtener_valor_columna(fila, ['PRODUCTO', 'Producto', 'producto'])
            cantidad_raw = self._obtener_valor_columna(
                fila, ['CANTIDAD NETA', 'Cantidad Neta', 'cantidad neta', 'CANTIDAD_NETA']
            )

            # Saltar filas sin datos suficientes
            if not dia_raw or not producto_raw:
                filas_ignoradas += 1
                continue

            # Parsear fecha
            fecha_str = self._parsear_fecha(dia_raw)
            if not fecha_str:
                filas_ignoradas += 1
                continue

            # Normalizar producto
            producto_norm = normalizar_texto(str(producto_raw))
            if not producto_norm:
                filas_ignoradas += 1
                continue

            # Parsear cantidad neta
            cantidad = self._parsear_cantidad(cantidad_raw)

            # Construir clave e indexar (sumar si hay duplicados)
            clave = (fecha_str, producto_norm)
            if clave in indice:
                indice[clave] += cantidad
            else:
                indice[clave] = cantidad

            filas_procesadas += 1

        logger.info(
            f"Indice Google Sheets: {len(indice)} entradas unicas "
            f"({filas_procesadas} filas procesadas, {filas_ignoradas} ignoradas)"
        )
        return indice

    def _obtener_valor_columna(self, fila: Dict, nombres_posibles: List[str]) -> Optional[str]:
        """Buscar valor en la fila por multiples nombres posibles de columna."""
        for nombre in nombres_posibles:
            if nombre in fila:
                valor = fila[nombre]
                if valor is not None and str(valor).strip():
                    return str(valor).strip()
        return None

    def _parsear_fecha(self, valor: str) -> Optional[str]:
        """
        Parsear fecha desde string a formato YYYY-MM-DD.
        Soporta multiples formatos comunes de Google Sheets.
        """
        if not valor:
            return None

        valor = str(valor).strip()

        # Formatos comunes de fecha
        formatos = [
            '%Y-%m-%d',         # 2026-02-16
            '%d/%m/%Y',         # 16/02/2026
            '%m/%d/%Y',         # 02/16/2026
            '%d-%m-%Y',         # 16-02-2026
            '%Y/%m/%d',         # 2026/02/16
            '%d/%m/%y',         # 16/02/26
        ]

        for fmt in formatos:
            try:
                dt = datetime.strptime(valor, fmt)
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue

        # Intentar con formato de Google Sheets serial date (numero)
        try:
            serial = float(valor)
            if 40000 < serial < 60000:  # Rango razonable para fechas 2009-2063
                # Google Sheets usa epoch 1899-12-30
                base = datetime(1899, 12, 30)
                dt = base + timedelta(days=int(serial))
                return dt.strftime('%Y-%m-%d')
        except (ValueError, TypeError):
            pass

        logger.debug(f"No se pudo parsear fecha: '{valor}'")
        return None

    def _parsear_cantidad(self, valor) -> Decimal:
        """Parsear cantidad neta desde valor de celda a Decimal."""
        if valor is None or str(valor).strip() == '':
            return Decimal('0')

        try:
            # Limpiar formato: quitar comas de miles, espacios
            texto = str(valor).replace(',', '').replace(' ', '').strip()
            return Decimal(texto)
        except (InvalidOperation, ValueError):
            logger.debug(f"No se pudo parsear cantidad: '{valor}'")
            return Decimal('0')
