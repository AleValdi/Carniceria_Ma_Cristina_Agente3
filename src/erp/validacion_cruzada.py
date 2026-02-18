"""
Validacion cruzada de remisiones pendientes.

Antes de registrar una factura directamente (Serie F sin consolidacion),
verifica si el proveedor tiene remisiones pendientes (Serie R) que podrian
corresponder a la factura. Esto previene registros duplicados cuando el
Agente 2 clasifica incorrectamente una factura como "Sin Remision".

Clasificacion:
- SEGURO: Sin remisiones pendientes del proveedor -> registrar normalmente
- BLOQUEAR: Proveedor tiene remisiones pendientes -> NO registrar
  (politica conservadora: preferible no registrar que arriesgar duplicado)
"""
from datetime import datetime
from typing import List, Optional

from loguru import logger

from config.settings import settings, sav7_config
from src.erp.sav7_connector import SAV7Connector
from src.erp.models import RemisionPendiente, ResultadoValidacion, ProveedorERP
from src.sat.models import Factura


class ValidadorRemisiones:
    """
    Validacion cruzada: verifica si un proveedor tiene remisiones pendientes
    que podrian corresponder a la factura CFDI antes de permitir el registro directo.
    """

    def __init__(self, connector: Optional[SAV7Connector] = None):
        self.connector = connector or SAV7Connector()
        self.config = sav7_config

    def obtener_remisiones_pendientes(self, clave_proveedor: str) -> List[RemisionPendiente]:
        """
        Obtener remisiones Serie R pendientes de un proveedor.

        Args:
            clave_proveedor: Clave del proveedor en SAVProveedor (ej: '001648')

        Returns:
            Lista de RemisionPendiente ordenadas por fecha DESC
        """
        query = f"""
            SELECT Serie, NumRec, Fecha, Total, Estatus, Factura, Proveedor
            FROM {self.config.tabla_recepciones}
            WHERE Proveedor = ?
              AND Serie = 'R'
              AND Estatus != 'Consolidada'
              AND Consolida = 0
            ORDER BY Fecha DESC
        """

        try:
            resultados = self.connector.execute_custom_query(query, (clave_proveedor,))

            if not resultados:
                return []

            remisiones = []
            for row in resultados:
                remision = RemisionPendiente(
                    serie=row['Serie'].strip() if row['Serie'] else 'R',
                    num_rec=int(row['NumRec']),
                    fecha=row['Fecha'],
                    total=float(row['Total'] or 0),
                    estatus=row['Estatus'].strip() if row['Estatus'] else '',
                    factura=row['Factura'].strip() if row['Factura'] else '',
                    proveedor=row['Proveedor'].strip() if row['Proveedor'] else '',
                )
                remisiones.append(remision)

            logger.debug(
                f"Proveedor {clave_proveedor}: {len(remisiones)} remisiones pendientes"
            )
            return remisiones

        except Exception as e:
            logger.error(
                f"Error al buscar remisiones pendientes del proveedor "
                f"{clave_proveedor}: {e}"
            )
            raise

    def validar_antes_de_registro(
        self,
        factura: Factura,
        proveedor: ProveedorERP,
    ) -> ResultadoValidacion:
        """
        Validar si es seguro registrar una factura directamente.

        Compara la factura CFDI contra remisiones pendientes del proveedor.
        Si encuentra una remision con monto y fecha similares, bloquea el
        registro para evitar duplicados.

        Args:
            factura: Factura CFDI parseada
            proveedor: Proveedor encontrado en SAVProveedor

        Returns:
            ResultadoValidacion con clasificacion SEGURO, REVISAR o BLOQUEAR
        """
        # Toggle: si la validacion esta desactivada, siempre es SEGURO
        if not settings.validar_remisiones_pendientes:
            logger.debug("Validacion cruzada desactivada por configuracion")
            return ResultadoValidacion(
                clasificacion='SEGURO',
                mensaje='Validacion cruzada desactivada'
            )

        # Edge case: factura con total 0 no tiene riesgo de duplicado
        total_factura = float(factura.total)
        if total_factura == 0:
            logger.debug("Factura con total $0 - sin riesgo de duplicado")
            return ResultadoValidacion(
                clasificacion='SEGURO',
                mensaje='Factura con total $0.00'
            )

        # Obtener remisiones pendientes del proveedor
        remisiones = self.obtener_remisiones_pendientes(proveedor.clave)

        if not remisiones:
            logger.debug(
                f"Proveedor {proveedor.clave} sin remisiones pendientes -> SEGURO"
            )
            return ResultadoValidacion(
                clasificacion='SEGURO',
                total_remisiones_pendientes=0,
                mensaje='Sin remisiones pendientes'
            )

        # Calcular diferencias de monto y fecha para cada remision
        fecha_factura = factura.fecha_emision
        mejor_candidato = None  # type: Optional[RemisionPendiente]
        menor_diferencia_monto = float('inf')

        for remision in remisiones:
            # Calcular diferencia de monto
            remision.diferencia_monto_pct = self._calcular_diferencia_monto(
                remision.total, total_factura
            )

            # Calcular diferencia de dias
            if remision.fecha and fecha_factura:
                try:
                    fecha_rem = remision.fecha
                    if isinstance(fecha_rem, datetime):
                        fecha_rem = fecha_rem.date()
                    fecha_fac = fecha_factura
                    if isinstance(fecha_fac, datetime):
                        fecha_fac = fecha_fac.date()
                    remision.diferencia_dias = abs((fecha_rem - fecha_fac).days)
                except (TypeError, AttributeError):
                    remision.diferencia_dias = 999  # Sin fecha, no matchea
            else:
                remision.diferencia_dias = 999

            # Rastrear el mejor candidato (menor diferencia de monto)
            if remision.diferencia_monto_pct < menor_diferencia_monto:
                menor_diferencia_monto = remision.diferencia_monto_pct
                mejor_candidato = remision

        # Clasificar
        tolerancia_monto = settings.tolerancia_monto_validacion
        dias_rango = settings.dias_rango_validacion

        if (mejor_candidato and
                mejor_candidato.diferencia_monto_pct <= tolerancia_monto and
                mejor_candidato.diferencia_dias <= dias_rango):
            # BLOQUEAR: hay una remision muy similar
            mensaje = (
                f"Remision pendiente similar: R-{mejor_candidato.num_rec} "
                f"(${mejor_candidato.total:,.2f}, "
                f"dif monto {mejor_candidato.diferencia_monto_pct:.1f}%, "
                f"dif fecha {mejor_candidato.diferencia_dias} dias). "
                f"Total remisiones pendientes del proveedor: {len(remisiones)}"
            )
            logger.warning(
                f"BLOQUEAR: Proveedor {proveedor.clave} tiene remision similar "
                f"R-{mejor_candidato.num_rec}"
            )
            return ResultadoValidacion(
                clasificacion='BLOQUEAR',
                total_remisiones_pendientes=len(remisiones),
                remision_similar=mejor_candidato,
                remisiones_pendientes=remisiones,
                mensaje=mensaje,
            )
        else:
            # BLOQUEAR: hay remisiones pendientes aunque ninguna matchee en monto+fecha.
            # Politica conservadora: preferible no registrar y reportar para revision
            # manual, que arriesgar un duplicado si Agente 2 consolida despues.
            if mejor_candidato:
                detalle_mejor = (
                    f"Mejor candidato: R-{mejor_candidato.num_rec} "
                    f"(${mejor_candidato.total:,.2f}, "
                    f"dif monto {mejor_candidato.diferencia_monto_pct:.1f}%, "
                    f"dif fecha {mejor_candidato.diferencia_dias} dias)"
                )
            else:
                detalle_mejor = "Sin candidato cercano"

            mensaje = (
                f"Proveedor tiene {len(remisiones)} remisiones pendientes "
                f"(ninguna similar en monto/fecha). {detalle_mejor}"
            )
            logger.warning(
                f"BLOQUEAR: Proveedor {proveedor.clave} tiene "
                f"{len(remisiones)} remisiones pendientes"
            )
            return ResultadoValidacion(
                clasificacion='BLOQUEAR',
                total_remisiones_pendientes=len(remisiones),
                remision_similar=mejor_candidato,
                remisiones_pendientes=remisiones,
                mensaje=mensaje,
            )

    @staticmethod
    def _calcular_diferencia_monto(total_remision: float, total_factura: float) -> float:
        """
        Calcular diferencia porcentual entre dos montos.

        Args:
            total_remision: Total de la remision
            total_factura: Total de la factura CFDI

        Returns:
            Diferencia en porcentaje (0.0 = identico, 100.0 = completamente distinto)
        """
        if total_factura == 0:
            return 100.0  # Siempre "diferente" para facturas con total $0
        return abs(total_remision - total_factura) / total_factura * 100
