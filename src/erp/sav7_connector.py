"""
Conector para el ERP SAV7 (SQL Server)
Maneja la conexion y consultas basicas a la base de datos
"""
from typing import Optional, List, Dict, Any
from loguru import logger

from config.database import DatabaseConnection, DatabaseConfig
from config.settings import sav7_config, SAV7Config


class SAV7Connector:
    """Conector principal para SAV7"""

    def __init__(
        self,
        db_config: Optional[DatabaseConfig] = None,
        sav7_cfg: Optional[SAV7Config] = None
    ):
        self.db = DatabaseConnection(db_config)
        self.config = sav7_cfg or sav7_config

    def test_connection(self) -> bool:
        """Probar conexion a la base de datos"""
        try:
            result = self.db.test_connection()
            if result:
                logger.info("Conexion a SAV7 exitosa")
            else:
                logger.error("No se pudo conectar a SAV7")
            return result
        except Exception as e:
            logger.error(f"Error al probar conexion: {e}")
            return False

    def execute_custom_query(
        self,
        query: str,
        params: tuple = ()
    ) -> List[Dict[str, Any]]:
        """
        Ejecutar query personalizado

        Args:
            query: Query SQL a ejecutar
            params: Parametros para el query

        Returns:
            Lista de diccionarios con los resultados
        """
        try:
            return self.db.execute_query(query, params)
        except Exception as e:
            logger.error(f"Error al ejecutar query: {e}")
            raise

    def close(self):
        """Cerrar conexion"""
        self.db.disconnect()
