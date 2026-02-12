"""
Configuración de conexión a base de datos SQL Server (SAV7)
"""
import os
import tempfile
from dataclasses import dataclass
from typing import Optional
import pyodbc
from contextlib import contextmanager

# Fix para OpenSSL 3.x en macOS: permite TLS legacy para ODBC Driver 18
if 'ODBC Driver 18' in os.getenv('DB_DRIVER', '') and not os.environ.get('OPENSSL_CONF'):
    _openssl_cnf = os.path.join(tempfile.gettempdir(), 'agente3_openssl.cnf')
    if not os.path.exists(_openssl_cnf):
        with open(_openssl_cnf, 'w') as f:
            f.write(
                "openssl_conf = openssl_init\n"
                "[openssl_init]\nssl_conf = ssl_sect\n"
                "[ssl_sect]\nsystem_default = system_default_sect\n"
                "[system_default_sect]\nMinProtocol = TLSv1\n"
                "CipherString = DEFAULT@SECLEVEL=0\n"
            )
    os.environ['OPENSSL_CONF'] = _openssl_cnf


@dataclass
class DatabaseConfig:
    """Configuración de conexión a SQL Server"""

    server: str = ""
    database: str = ""
    username: str = ""
    password: str = ""
    driver: str = "{ODBC Driver 17 for SQL Server}"
    port: int = 1433
    trusted_connection: bool = False  # True para autenticación Windows
    timeout: int = 30

    @classmethod
    def from_env(cls) -> 'DatabaseConfig':
        """Crear configuración desde variables de entorno"""
        return cls(
            server=os.getenv('DB_SERVER', 'localhost'),
            database=os.getenv('DB_DATABASE', 'SAV7'),
            username=os.getenv('DB_USERNAME', ''),
            password=os.getenv('DB_PASSWORD', ''),
            driver=os.getenv('DB_DRIVER', '{ODBC Driver 17 for SQL Server}'),
            port=int(os.getenv('DB_PORT', '1433')),
            trusted_connection=os.getenv('DB_TRUSTED_CONNECTION', 'false').lower() == 'true',
            timeout=int(os.getenv('DB_TIMEOUT', '30')),
        )

    def get_connection_string(self) -> str:
        """Generar string de conexión para pyodbc"""
        if self.trusted_connection:
            # Autenticación Windows
            return (
                f"DRIVER={self.driver};"
                f"SERVER={self.server},{self.port};"
                f"DATABASE={self.database};"
                f"Trusted_Connection=yes;"
                f"Connection Timeout={self.timeout};"
            )
        else:
            # Autenticación SQL Server
            return (
                f"DRIVER={self.driver};"
                f"SERVER={self.server},{self.port};"
                f"DATABASE={self.database};"
                f"UID={self.username};"
                f"PWD={self.password};"
                f"TrustServerCertificate=yes;"
                f"Connection Timeout={self.timeout};"
            )


class DatabaseConnection:
    """Manejador de conexiones a SQL Server"""

    def __init__(self, config: Optional[DatabaseConfig] = None):
        self.config = config or DatabaseConfig.from_env()
        self._connection: Optional[pyodbc.Connection] = None

    def connect(self) -> pyodbc.Connection:
        """Establecer conexión a la base de datos"""
        if self._connection is None or self._connection.closed:
            try:
                self._connection = pyodbc.connect(
                    self.config.get_connection_string(),
                    autocommit=False
                )
            except pyodbc.Error as e:
                raise ConnectionError(f"Error al conectar a SQL Server: {e}")
        return self._connection

    def disconnect(self):
        """Cerrar conexión"""
        if self._connection and not self._connection.closed:
            self._connection.close()
            self._connection = None

    @contextmanager
    def get_cursor(self):
        """Context manager para obtener un cursor"""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()

    def execute_query(self, query: str, params: tuple = ()) -> list:
        """Ejecutar query y retornar resultados como lista de diccionarios"""
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            columns = [column[0] for column in cursor.description]
            results = []
            for row in cursor.fetchall():
                results.append(dict(zip(columns, row)))
            return results

    def execute_scalar(self, query: str, params: tuple = ()):
        """Ejecutar query y retornar un solo valor"""
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
            return row[0] if row else None

    def test_connection(self) -> bool:
        """Probar la conexión a la base de datos"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute("SELECT 1")
                return True
        except Exception:
            return False


# Instancia global de conexión
db_connection = DatabaseConnection()
