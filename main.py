"""
Agente 3 - Registro Directo de Facturas CFDI sin Remision
Punto de entrada principal (CLI)

Uso:
    python main.py                        # Procesar todos los XMLs
    python main.py --dry-run              # Simular sin escribir en BD
    python main.py --archivo factura.xml  # Procesar un solo XML
    python main.py --test-conexion        # Probar conexion BD
    python main.py --explorar-catalogo    # Estadisticas del catalogo
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from loguru import logger

from config.settings import settings, sav7_config, BASE_DIR
from src.sat.xml_parser import CFDIParser
from src.sat.models import Factura, TipoComprobante
from src.erp.sav7_connector import SAV7Connector
from src.erp.proveedor_repo import ProveedorRepository
from src.erp.producto_repo import ProductoRepository
from src.erp.registro_directo import RegistradorDirecto
from src.erp.registro_nc import RegistradorNC
from src.erp.factura_repo import FacturaRepository
from src.erp.models import ResultadoRegistro, ResultadoMatchProducto, ResultadoValidacion
from src.erp.validacion_cruzada import ValidadorRemisiones
from src.matching.cache_productos import CacheProductos
from src.matching.historial_compras import HistorialCompras
from src.matching.producto_matcher import ProductoMatcher
from src.reports.excel_generator import ExcelGenerator


def configurar_logger():
    """Configurar loguru con formato y archivos de log"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=settings.log_format,
    )
    log_file = settings.logs_dir / f"agente3_{datetime.now():%Y%m%d}.log"
    logger.add(
        str(log_file),
        level="DEBUG",
        format=settings.log_format,
        rotation="10 MB",
        retention="30 days",
    )


def test_conexion():
    """Probar conexion a la BD y mostrar info basica"""
    logger.info("Probando conexion a la base de datos...")
    connector = SAV7Connector()

    if connector.test_connection():
        print("Conexion exitosa a la base de datos")

        # Contar registros en tablas principales
        tablas = [
            (sav7_config.tabla_recepciones, "Recepciones (SAVRecC)"),
            (sav7_config.tabla_detalle_recepciones, "Detalle (SAVRecD)"),
            (sav7_config.tabla_proveedores, "Proveedores"),
            (sav7_config.tabla_productos, "Productos"),
        ]
        for tabla, desc in tablas:
            try:
                resultado = connector.execute_custom_query(
                    f"SELECT COUNT(*) as total FROM {tabla}"
                )
                total = resultado[0]['total'] if resultado else 0
                print(f"  {desc}: {total:,} registros")
            except Exception as e:
                print(f"  {desc}: Error - {e}")

        # Contar facturas F existentes
        try:
            resultado = connector.execute_custom_query(
                f"SELECT COUNT(*) as total FROM {sav7_config.tabla_recepciones} WHERE Serie = 'F'"
            )
            total_f = resultado[0]['total'] if resultado else 0
            print(f"  Facturas Serie F: {total_f:,}")
        except Exception:
            pass

        connector.close()
        return True
    else:
        print("ERROR: No se pudo conectar a la base de datos")
        connector.close()
        return False


def explorar_catalogo():
    """Mostrar estadisticas del catalogo de productos"""
    logger.info("Cargando catalogo de productos...")
    connector = SAV7Connector()

    if not connector.test_connection():
        print("ERROR: No se pudo conectar a la base de datos")
        return

    repo = ProductoRepository(connector)
    productos = repo.cargar_catalogo()
    cache = CacheProductos()
    cache.cargar(productos)

    print(f"\n{'='*60}")
    print(f"CATALOGO DE PRODUCTOS SAV7")
    print(f"{'='*60}")
    print(f"Total productos: {cache.total_productos:,}")

    # Estadisticas por codigo SAT
    codigos_sat = {}
    for p in productos:
        if p.codigo_sat:
            codigos_sat[p.codigo_sat] = codigos_sat.get(p.codigo_sat, 0) + 1
    print(f"Codigos SAT unicos: {len(codigos_sat):,}")

    # Top 10 codigos SAT mas frecuentes
    top_sat = sorted(codigos_sat.items(), key=lambda x: x[1], reverse=True)[:10]
    print(f"\nTop 10 codigos SAT:")
    for codigo, cuenta in top_sat:
        print(f"  {codigo}: {cuenta} productos")

    # Estadisticas por familia
    familias = {}
    for p in productos:
        if p.familia1:
            familias[p.familia1] = familias.get(p.familia1, 0) + 1
    top_familias = sorted(familias.items(), key=lambda x: x[1], reverse=True)[:10]
    print(f"\nTop 10 familias:")
    for familia, cuenta in top_familias:
        print(f"  {familia}: {cuenta} productos")

    # Productos con/sin IVA
    con_iva = sum(1 for p in productos if p.porc_iva > 0)
    sin_iva = len(productos) - con_iva
    print(f"\nCon IVA: {con_iva:,} | Sin IVA: {sin_iva:,}")

    # Servicios
    servicios = sum(1 for p in productos if p.servicio)
    print(f"Servicios: {servicios:,} | Productos: {len(productos) - servicios:,}")

    connector.close()


def procesar_factura(
    factura: Factura,
    proveedor_repo: ProveedorRepository,
    matcher: ProductoMatcher,
    registrador: RegistradorDirecto,
    registrador_nc: RegistradorNC,
    validador: ValidadorRemisiones,
    dry_run: bool = False,
) -> Tuple[ResultadoRegistro, List[ResultadoMatchProducto]]:
    """
    Procesar una factura individual (Ingreso o Egreso/NC).

    Returns:
        Tupla de (ResultadoRegistro, lista de matches)
    """
    uuid = factura.uuid.upper()

    # Bifurcar: Egreso (Nota de Credito) vs Ingreso (Factura)
    if factura.tipo_comprobante == TipoComprobante.EGRESO:
        return procesar_nota_credito(
            factura, proveedor_repo, matcher, registrador_nc, dry_run
        )
    elif factura.tipo_comprobante != TipoComprobante.INGRESO:
        return ResultadoRegistro(
            exito=False,
            factura_uuid=uuid,
            total_conceptos=len(factura.conceptos),
            mensaje=f"Tipo de comprobante no soportado: {factura.tipo_comprobante.value}",
            error="TIPO_NO_SOPORTADO"
        ), []

    # --- Flujo INGRESO (sin cambios) ---

    # Verificar que tiene conceptos
    if not factura.conceptos:
        return ResultadoRegistro(
            exito=False,
            factura_uuid=uuid,
            total_conceptos=0,
            mensaje="La factura no tiene conceptos",
            error="SIN_CONCEPTOS"
        ), []

    # Buscar proveedor por RFC
    proveedor = proveedor_repo.buscar_por_rfc(factura.rfc_emisor)
    if not proveedor:
        return ResultadoRegistro(
            exito=False,
            factura_uuid=uuid,
            total_conceptos=len(factura.conceptos),
            mensaje=f"Proveedor no encontrado: RFC {factura.rfc_emisor} ({factura.nombre_emisor})",
            error="PROVEEDOR_NO_ENCONTRADO"
        ), []

    # --- Validacion cruzada: verificar remisiones pendientes ---
    validacion = validador.validar_antes_de_registro(factura, proveedor)

    if validacion.clasificacion == 'BLOQUEAR':
        r_sim = validacion.remision_similar
        logger.warning(
            f"  BLOQUEADA: Remision pendiente R-{r_sim.num_rec} "
            f"(${r_sim.total:,.2f}, dif {r_sim.diferencia_monto_pct:.1f}%, "
            f"{r_sim.diferencia_dias} dias)"
        )
        return ResultadoRegistro(
            exito=False,
            factura_uuid=uuid,
            total_conceptos=len(factura.conceptos),
            mensaje=(
                f"Remision pendiente similar: R-{r_sim.num_rec} "
                f"(${r_sim.total:,.2f}, dif {r_sim.diferencia_monto_pct:.1f}%, "
                f"{r_sim.diferencia_dias} dias). "
                f"Total pendientes: {validacion.total_remisiones_pendientes}"
            ),
            error="REMISION_PENDIENTE"
        ), []
    elif validacion.clasificacion == 'REVISAR':
        logger.info(
            f"  AVISO: {validacion.total_remisiones_pendientes} remisiones "
            f"pendientes (ninguna similar en monto/fecha)"
        )
    # SEGURO -> continua normalmente

    # Matchear conceptos
    matches = matcher.matchear_lote(factura.conceptos, proveedor.clave)

    # Registrar en ERP
    resultado = registrador.registrar(factura, proveedor, matches, dry_run=dry_run)

    # Agregar advertencia de validacion si REVISAR
    if validacion.clasificacion == 'REVISAR':
        resultado.advertencia_validacion = validacion.mensaje

    return resultado, matches


def procesar_nota_credito(
    factura: Factura,
    proveedor_repo: ProveedorRepository,
    matcher: ProductoMatcher,
    registrador_nc: RegistradorNC,
    dry_run: bool = False,
) -> Tuple[ResultadoRegistro, List[ResultadoMatchProducto]]:
    """
    Procesar una Nota de Credito (CFDI tipo Egreso).

    Returns:
        Tupla de (ResultadoRegistro, lista de matches)
    """
    uuid = factura.uuid.upper()

    logger.info(f"  Tipo: NOTA DE CREDITO (Egreso)")
    if factura.cfdi_relacionados:
        logger.info(
            f"  CFDI Relacionado: {factura.cfdi_relacionados[0].uuid[:12]}... "
            f"(TipoRelacion={factura.cfdi_relacionados[0].tipo_relacion})"
        )

    # Verificar que tiene CFDI relacionados
    if not factura.cfdi_relacionados:
        return ResultadoRegistro(
            exito=False,
            factura_uuid=uuid,
            total_conceptos=len(factura.conceptos),
            es_nota_credito=True,
            mensaje="NC sin CFDI relacionado en el XML",
            error="SIN_CFDI_RELACIONADO"
        ), []

    # Verificar que tiene conceptos
    if not factura.conceptos:
        return ResultadoRegistro(
            exito=False,
            factura_uuid=uuid,
            total_conceptos=0,
            es_nota_credito=True,
            mensaje="La NC no tiene conceptos",
            error="SIN_CONCEPTOS"
        ), []

    # Buscar proveedor por RFC
    proveedor = proveedor_repo.buscar_por_rfc(factura.rfc_emisor)
    if not proveedor:
        return ResultadoRegistro(
            exito=False,
            factura_uuid=uuid,
            total_conceptos=len(factura.conceptos),
            es_nota_credito=True,
            mensaje=f"Proveedor no encontrado: RFC {factura.rfc_emisor} ({factura.nombre_emisor})",
            error="PROVEEDOR_NO_ENCONTRADO"
        ), []

    # Matchear conceptos contra catalogo ERP (para determinar DEVOLUCIONES vs DESCUENTOS)
    matches = matcher.matchear_lote(factura.conceptos, proveedor.clave)

    # Registrar NC en ERP
    resultado = registrador_nc.registrar(factura, proveedor, matches, dry_run=dry_run)

    return resultado, matches


def mover_xml(archivo_xml: str, resultado: ResultadoRegistro):
    """Mover XML a carpeta correspondiente segun resultado"""
    ruta_origen = Path(archivo_xml)
    if not ruta_origen.exists():
        return

    if resultado.exito:
        if resultado.registro_parcial:
            destino = settings.parciales_dir
        else:
            destino = settings.processed_dir
    else:
        destino = settings.fallidos_dir

    ruta_destino = destino / ruta_origen.name
    # Evitar sobreescritura
    if ruta_destino.exists():
        nombre_sin_ext = ruta_origen.stem
        ext = ruta_origen.suffix
        timestamp = datetime.now().strftime('%H%M%S')
        ruta_destino = destino / f"{nombre_sin_ext}_{timestamp}{ext}"

    try:
        shutil.move(str(ruta_origen), str(ruta_destino))
        logger.debug(f"XML movido a: {ruta_destino}")
    except Exception as e:
        logger.warning(f"No se pudo mover XML {ruta_origen}: {e}")


def procesar_lote(dry_run: bool = False, archivo_unico: str = None):
    """Procesar un lote de XMLs o un archivo individual"""
    inicio = datetime.now()

    logger.info(f"{'='*60}")
    logger.info(f"AGENTE 3 - Registro Directo de Facturas CFDI")
    logger.info(f"{'='*60}")
    logger.info(f"Modo: {'DRY-RUN (simulacion)' if dry_run else 'PRODUCCION'}")
    logger.info(f"Inicio: {inicio:%Y-%m-%d %H:%M:%S}")

    # Inicializar componentes
    logger.info("Inicializando componentes...")
    connector = SAV7Connector()

    if not connector.test_connection():
        logger.error("No se pudo conectar a la base de datos. Abortando.")
        return

    proveedor_repo = ProveedorRepository(connector)
    producto_repo = ProductoRepository(connector)
    registrador = RegistradorDirecto(connector)
    factura_repo = FacturaRepository(connector)
    registrador_nc = RegistradorNC(connector, factura_repo)
    validador = ValidadorRemisiones(connector)

    # Cargar catalogo de productos
    logger.info("Cargando catalogo de productos...")
    productos = producto_repo.cargar_catalogo()
    cache = CacheProductos()
    cache.cargar(productos)

    # Inicializar historial y matcher
    historial = HistorialCompras(connector, cache)
    matcher = ProductoMatcher(cache, historial)

    # Parsear XMLs
    parser = CFDIParser()
    if archivo_unico:
        ruta = Path(archivo_unico)
        if not ruta.is_absolute():
            ruta = settings.input_dir / ruta
        facturas = []
        factura = parser.parse_archivo(ruta)
        if factura:
            facturas.append(factura)
        else:
            logger.error(f"No se pudo parsear el archivo: {ruta}")
    else:
        logger.info(f"Escaneando directorio: {settings.input_dir}")
        facturas = parser.parse_directorio(settings.input_dir)

    if not facturas:
        logger.warning("No se encontraron facturas XML para procesar")
        connector.close()
        return

    # Ordenar: Ingresos primero, Egresos despues (NCs necesitan que su factura F ya exista)
    facturas.sort(key=lambda f: (0 if f.tipo_comprobante == 'I' else 1))

    logger.info(f"Facturas a procesar: {len(facturas)}")

    # Procesar cada factura
    resultados: List[ResultadoRegistro] = []
    todos_matches: List[Tuple[Factura, List[ResultadoMatchProducto]]] = []

    for i, factura in enumerate(facturas, 1):
        logger.info(f"\n--- Factura {i}/{len(facturas)}: {factura.identificador} ---")
        logger.info(f"  Emisor: {factura.nombre_emisor} ({factura.rfc_emisor})")
        logger.info(f"  Total: ${float(factura.total):,.2f} | Conceptos: {len(factura.conceptos)}")

        resultado, matches = procesar_factura(
            factura, proveedor_repo, matcher, registrador, registrador_nc,
            validador, dry_run
        )
        resultados.append(resultado)
        todos_matches.append((factura, matches))

        # Log resultado
        if resultado.exito:
            tipo_txt = f" NC {resultado.tipo_nc}" if resultado.es_nota_credito else ""
            parcial_txt = " (PARCIAL)" if resultado.registro_parcial else ""
            vinc_txt = (
                f" -> {resultado.factura_vinculada_erp}"
                if resultado.factura_vinculada_erp else ""
            )
            logger.info(
                f"  RESULTADO: EXITOSO{tipo_txt}{parcial_txt} -> "
                f"{resultado.numero_factura_erp}{vinc_txt} "
                f"({resultado.conceptos_matcheados_count}/{resultado.total_conceptos} conceptos)"
            )
        else:
            logger.warning(f"  RESULTADO: FALLIDO - {resultado.mensaje}")

        # Mover XML
        if not dry_run and factura.archivo_xml:
            mover_xml(factura.archivo_xml, resultado)

    # Resumen
    fin = datetime.now()
    duracion = (fin - inicio).total_seconds()

    # Separar facturas Ingreso y NCs Egreso
    res_ingreso = [r for r in resultados if not r.es_nota_credito]
    res_nc = [r for r in resultados if r.es_nota_credito]

    exitosos = sum(1 for r in res_ingreso if r.exito and not r.registro_parcial)
    parciales = sum(1 for r in res_ingreso if r.exito and r.registro_parcial)
    fallidos_ingreso = sum(1 for r in res_ingreso if not r.exito)

    nc_exitosas = sum(1 for r in res_nc if r.exito)
    nc_fallidas = sum(1 for r in res_nc if not r.exito)

    logger.info(f"\n{'='*60}")
    logger.info(f"RESUMEN DE EJECUCION")
    logger.info(f"{'='*60}")
    logger.info(f"Total procesadas: {len(resultados)}")
    if res_ingreso:
        logger.info(f"  Facturas Ingreso: {len(res_ingreso)}")
        logger.info(f"    Exitosas:  {exitosos}")
        logger.info(f"    Parciales: {parciales}")
        logger.info(f"    Fallidas:  {fallidos_ingreso}")
    if res_nc:
        logger.info(f"  Notas de Credito: {len(res_nc)}")
        logger.info(f"    Exitosas:  {nc_exitosas}")
        logger.info(f"    Fallidas:  {nc_fallidas}")
    logger.info(f"Duracion: {duracion:.1f} segundos")
    logger.info(f"Modo: {'DRY-RUN' if dry_run else 'PRODUCCION'}")

    # Generar reporte Excel
    logger.info("Generando reporte Excel...")
    try:
        generador = ExcelGenerator(settings.output_dir)
        ruta_reporte = generador.generar(
            resultados=resultados,
            facturas=facturas,
            todos_matches=todos_matches,
            dry_run=dry_run,
            duracion_segundos=duracion,
        )
        logger.info(f"Reporte generado: {ruta_reporte}")
    except Exception as e:
        logger.error(f"Error al generar reporte: {e}")

    connector.close()

    # Imprimir resumen en consola
    print(f"\n{'='*60}")
    print(f"Agente 3 - Registro Directo CFDI")
    print(f"{'='*60}")
    print(f"Total procesadas: {len(resultados)}")
    if res_ingreso:
        print(f"  Facturas Ingreso: {len(res_ingreso)} (exitosas: {exitosos}, parciales: {parciales}, fallidas: {fallidos_ingreso})")
    if res_nc:
        print(f"  Notas de Credito: {len(res_nc)} (exitosas: {nc_exitosas}, fallidas: {nc_fallidas})")
    print(f"Duracion: {duracion:.1f}s")
    if dry_run:
        print("MODO DRY-RUN: No se realizaron cambios en la BD")


def main():
    """Punto de entrada principal"""
    parser = argparse.ArgumentParser(
        description="Agente 3 - Registro Directo de Facturas CFDI sin Remision"
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Simular sin escribir en la base de datos'
    )
    parser.add_argument(
        '--archivo',
        type=str,
        help='Procesar un solo archivo XML (nombre o ruta completa)'
    )
    parser.add_argument(
        '--test-conexion',
        action='store_true',
        help='Probar conexion a la base de datos'
    )
    parser.add_argument(
        '--explorar-catalogo',
        action='store_true',
        help='Mostrar estadisticas del catalogo de productos'
    )

    args = parser.parse_args()

    configurar_logger()

    if args.test_conexion:
        test_conexion()
    elif args.explorar_catalogo:
        explorar_catalogo()
    else:
        procesar_lote(
            dry_run=args.dry_run,
            archivo_unico=args.archivo,
        )


if __name__ == '__main__':
    main()
