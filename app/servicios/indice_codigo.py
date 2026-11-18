"""Índice local de fragmentos para búsqueda semántica bajo demanda."""

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import threading
from contextlib import closing
from pathlib import Path

import httpx

EXTENSIONES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".json", ".jsx", ".kt", ".md", ".php", ".py", ".rb",
    ".rs", ".sh", ".sql", ".swift", ".toml", ".ts", ".tsx", ".txt",
    ".vue", ".xml", ".yaml", ".yml",
}
MAX_ARCHIVO = 256 * 1024
MAX_FRAGMENTO = 1000
MAX_RESULTADOS = 5
MAX_MARGEN_LINEAS = 20
MAX_CARACTERES_LECTURA = 8000
RRF_K = 60


class IndiceCodigo:
    def __init__(
        self, proyecto: Path, directorio_indice: Path, url: str, modelo: str, clave: str,
        prefijo_documento: str = "", prefijo_consulta: str = "",
        modelo_peticion: str | None = None,
    ) -> None:
        self.proyecto = proyecto.resolve(strict=True)
        if not self.proyecto.is_dir():
            raise ValueError("MCP_WORKSPACE debe ser un directorio.")
        if not modelo:
            raise ValueError("Configura EMBEDDINGS_MODEL en el .env del servidor.")
        self.modelo = modelo
        self.modelo_peticion = modelo_peticion if modelo_peticion is not None else modelo
        self.url = url.rstrip("/") + "/v1/embeddings"
        self.clave = clave
        self.prefijo_documento = prefijo_documento
        self.prefijo_consulta = prefijo_consulta
        identificador = hashlib.sha256(str(self.proyecto).encode("utf-8")).hexdigest()
        self.ruta = directorio_indice / f"codigo-{identificador}.sqlite3"
        self._candado = threading.Lock()

    def _conexion(self) -> sqlite3.Connection:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        conexion = sqlite3.connect(self.ruta)
        try:
            conexion.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archivos (ruta TEXT PRIMARY KEY, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fragmentos (
                    ruta TEXT NOT NULL, inicio INTEGER NOT NULL, fin INTEGER NOT NULL,
                    texto TEXT NOT NULL, vector TEXT NOT NULL
                );
            """)
            self._asegurar_fts5(conexion)
        except Exception:
            conexion.close()
            raise
        return conexion

    @staticmethod
    def _asegurar_fts5(conexion: sqlite3.Connection) -> None:
        existe = conexion.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fragmentos_fts'"
        ).fetchone()
        if existe:
            return

        try:
            conexion.execute("BEGIN IMMEDIATE")
            existe = conexion.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fragmentos_fts'"
            ).fetchone()
            if existe:
                conexion.commit()
                return

            conexion.execute(
                "CREATE VIRTUAL TABLE fragmentos_fts USING "
                "fts5(texto, ruta, content='fragmentos', content_rowid='rowid')"
            )
            conexion.execute("""
                CREATE TRIGGER fragmentos_fts_insert AFTER INSERT ON fragmentos BEGIN
                    INSERT INTO fragmentos_fts(rowid, texto, ruta)
                    VALUES (new.rowid, new.texto, new.ruta);
                END
            """)
            conexion.execute("""
                CREATE TRIGGER fragmentos_fts_delete AFTER DELETE ON fragmentos BEGIN
                    INSERT INTO fragmentos_fts(fragmentos_fts, rowid, texto, ruta)
                    VALUES ('delete', old.rowid, old.texto, old.ruta);
                END
            """)
            conexion.execute("""
                CREATE TRIGGER fragmentos_fts_update AFTER UPDATE ON fragmentos BEGIN
                    INSERT INTO fragmentos_fts(fragmentos_fts, rowid, texto, ruta)
                    VALUES ('delete', old.rowid, old.texto, old.ruta);
                    INSERT INTO fragmentos_fts(rowid, texto, ruta)
                    VALUES (new.rowid, new.texto, new.ruta);
                END
            """)
            conexion.execute("INSERT INTO fragmentos_fts(fragmentos_fts) VALUES ('rebuild')")
            conexion.commit()
        except sqlite3.Error as error:
            conexion.rollback()
            if isinstance(error, sqlite3.OperationalError) and "fts5" in str(error).lower():
                raise RuntimeError("SQLite debe tener soporte FTS5 para buscar texto.") from error
            raise

    def _archivos_git(self) -> list[str]:
        try:
            resultado = subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=self.proyecto,
                # La entrada del proceso MCP pertenece al protocolo y no a Git.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError("Git es necesario para listar los archivos del proyecto.") from error
        except subprocess.CalledProcessError as error:
            detalle = error.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"No se pudo listar el proyecto con Git: {detalle}") from error
        return sorted(set(
            nombre.decode("utf-8", errors="surrogateescape")
            for nombre in resultado.stdout.split(b"\0") if nombre
        ))

    def _embedding(self, cliente: httpx.Client, texto: str) -> list[float]:
        headers = {"Authorization": f"Bearer {self.clave}"} if self.clave else {}
        try:
            respuesta = cliente.post(
                self.url,
                json={"model": self.modelo_peticion, "input": texto, "encoding_format": "float"},
                headers=headers,
            )
            respuesta.raise_for_status()
            datos = respuesta.json()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                f"El servidor de embeddings respondió {error.response.status_code}: "
                f"{error.response.text[:1000]}"
            ) from error
        except httpx.RequestError as error:
            raise RuntimeError(f"No se pudo conectar al servidor de embeddings: {error}") from error
        except ValueError as error:
            raise RuntimeError("El servidor de embeddings devolvió JSON inválido.") from error

        try:
            vector = datos["data"][0]["embedding"]
            if not isinstance(vector, list) or not vector or any(
                type(valor) not in (int, float) or not math.isfinite(valor)
                for valor in vector
            ):
                raise ValueError("vector inválido")
            norma = math.sqrt(sum(valor * valor for valor in vector))
            if not math.isfinite(norma) or norma == 0:
                raise ValueError("vector sin norma válida")
            return [valor / norma for valor in vector]
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
            raise RuntimeError("El servidor devolvió un vector de embeddings inválido.") from error

    @staticmethod
    def _fragmentos(texto: str):
        lineas = texto.splitlines(keepends=True)
        partes = []
        largo = 0
        inicio = 1
        for numero, linea in enumerate(lineas, 1):
            while linea:
                if not partes:
                    inicio = numero
                disponible = MAX_FRAGMENTO - largo
                parte, linea = linea[:disponible], linea[disponible:]
                partes.append(parte)
                largo += len(parte)
                if largo == MAX_FRAGMENTO:
                    yield inicio, numero, "".join(partes)
                    partes, largo = [], 0
        if partes:
            yield inicio, len(lineas), "".join(partes)

    def indexar(self, reconstruir: bool = False) -> dict:
        with self._candado, closing(self._conexion()) as conexion, conexion:
            metadata = dict(conexion.execute("SELECT clave, valor FROM metadata"))
            mismo_modelo = (
                not reconstruir
                and metadata.get("modelo") == self.modelo
                and metadata.get("prefijo_documento", "") == self.prefijo_documento
                and metadata.get("prefijo_consulta", "") == self.prefijo_consulta
            )
            anteriores = dict(conexion.execute("SELECT ruta, hash FROM archivos")) if mismo_modelo else {}
            actuales = {}
            cambios = {}
            omitidos = 0

            with httpx.Client(timeout=httpx.Timeout(None, connect=5), trust_env=False) as cliente:
                for nombre in self._archivos_git():
                    ruta_relativa = Path(nombre)
                    if (
                        ruta_relativa.suffix.lower() not in EXTENSIONES
                        or any(parte.lower().startswith(".env") for parte in ruta_relativa.parts)
                    ):
                        continue
                    ruta = self.proyecto / ruta_relativa
                    if ruta.is_symlink() or not ruta.resolve().is_relative_to(self.proyecto):
                        omitidos += 1
                        continue
                    try:
                        if ruta.stat().st_size > MAX_ARCHIVO:
                            omitidos += 1
                            continue
                        contenido = ruta.read_bytes()
                        texto = contenido.decode("utf-8")
                    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                        omitidos += 1
                        continue
                    if "\0" in texto:
                        omitidos += 1
                        continue
                    huella = hashlib.sha256(contenido).hexdigest()
                    actuales[nombre] = huella
                    if anteriores.get(nombre) == huella:
                        continue
                    cambios[nombre] = [
                        (
                            inicio, fin, fragmento,
                            json.dumps(self._embedding(cliente, self.prefijo_documento + fragmento)),
                        )
                        for inicio, fin, fragmento in self._fragmentos(texto)
                        if fragmento.strip()
                    ]

            eliminados = set(anteriores) - set(actuales)
            if not mismo_modelo:
                conexion.execute("DELETE FROM archivos")
                conexion.execute("DELETE FROM fragmentos")
            for nombre in eliminados | cambios.keys():
                conexion.execute("DELETE FROM archivos WHERE ruta = ?", (nombre,))
                conexion.execute("DELETE FROM fragmentos WHERE ruta = ?", (nombre,))
            for nombre, fragmentos in cambios.items():
                conexion.execute("INSERT INTO archivos VALUES (?, ?)", (nombre, actuales[nombre]))
                conexion.executemany(
                    "INSERT INTO fragmentos VALUES (?, ?, ?, ?, ?)",
                    [(nombre, inicio, fin, texto, vector) for inicio, fin, texto, vector in fragmentos],
                )
            conexion.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('modelo', ?)", (self.modelo,)
            )
            for clave, valor in (
                ("prefijo_documento", self.prefijo_documento),
                ("prefijo_consulta", self.prefijo_consulta),
            ):
                conexion.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (clave, valor))
            return {
                "archivos": len(actuales),
                "actualizados": len(cambios),
                "eliminados": len(eliminados),
                "omitidos": omitidos,
                "fragmentos": conexion.execute("SELECT COUNT(*) FROM fragmentos").fetchone()[0],
            }

    def buscar(self, consulta: str, limite: int = 3) -> list[dict]:
        if not isinstance(consulta, str) or not consulta.strip() or len(consulta) > MAX_FRAGMENTO:
            raise ValueError(f"La consulta debe tener entre 1 y {MAX_FRAGMENTO} caracteres.")
        if type(limite) is not int or not 1 <= limite <= MAX_RESULTADOS:
            raise ValueError(f"El límite debe estar entre 1 y {MAX_RESULTADOS}.")
        if not self.ruta.is_file():
            raise RuntimeError("Primero ejecuta la herramienta indexar_proyecto.")
        with closing(self._conexion()) as conexion:
            metadata = dict(conexion.execute("SELECT clave, valor FROM metadata"))
            if (
                metadata.get("modelo") != self.modelo
                or metadata.get("prefijo_documento", "") != self.prefijo_documento
                or metadata.get("prefijo_consulta", "") != self.prefijo_consulta
            ):
                raise RuntimeError("Cambió la configuración del modelo. Ejecuta indexar_proyecto.")
            with httpx.Client(timeout=httpx.Timeout(None, connect=5), trust_env=False) as cliente:
                vector = self._embedding(cliente, self.prefijo_consulta + consulta)
            conexion.execute("BEGIN")
            resultados = {}
            semanticos = []
            for rowid, ruta, inicio, fin, texto, vector_json in conexion.execute(
                "SELECT rowid, ruta, inicio, fin, texto, vector FROM fragmentos ORDER BY rowid"
            ):
                guardado = json.loads(vector_json)
                if len(vector) != len(guardado):
                    raise RuntimeError("La dimensión del modelo cambió. Indexa con reconstruir=true.")
                puntaje = sum(a * b for a, b in zip(vector, guardado))
                resultados[rowid] = {
                    "ruta": ruta, "linea_inicio": inicio, "linea_fin": fin,
                    "similitud": round(puntaje, 4), "texto": texto,
                }
                semanticos.append((rowid, puntaje))
            semanticos.sort(key=lambda resultado: (-resultado[1], resultado[0]))

            consulta_fts = self._consulta_fts(consulta)
            textuales = []
            if consulta_fts:
                textuales = [
                    fila[0] for fila in conexion.execute(
                        """
                        SELECT rowid
                        FROM fragmentos_fts
                        WHERE fragmentos_fts MATCH ?
                        ORDER BY bm25(fragmentos_fts), rowid
                        LIMIT ?
                        """,
                        (consulta_fts, MAX_RESULTADOS),
                    )
                ]
            conexion.commit()

        puestos = {}
        for rango, (rowid, _) in enumerate(semanticos[:MAX_RESULTADOS], 1):
            puestos[rowid] = puestos.get(rowid, 0.0) + 1 / (RRF_K + rango)
        for rango, rowid in enumerate(textuales, 1):
            puestos[rowid] = puestos.get(rowid, 0.0) + 1 / (RRF_K + rango)

        ordenados = sorted(
            puestos,
            key=lambda rowid: (
                -puestos[rowid],
                -resultados[rowid]["similitud"],
                resultados[rowid]["ruta"],
                resultados[rowid]["linea_inicio"],
            ),
        )
        return [resultados[rowid] for rowid in ordenados[:limite]]

    @staticmethod
    def _consulta_fts(consulta: str) -> str:
        tokens = re.findall(r"[^\W_]+", consulta, flags=re.UNICODE)
        if not tokens:
            return ""
        frase = '"' + " ".join(tokens) + '"'
        terminos = dict.fromkeys(tokens)
        return " OR ".join([frase, *(f'"{termino}"' for termino in terminos)])

    def leer_lineas(self, ruta: str, linea: int, margen: int = 5) -> dict:
        """Lee líneas de un archivo indexado, sin permitir salir del workspace."""
        if not isinstance(ruta, str) or not ruta:
            raise ValueError("La ruta debe ser un texto relativo.")
        if type(linea) is not int or linea < 1:
            raise ValueError("La línea debe ser un entero mayor o igual a 1.")
        if type(margen) is not int or not 0 <= margen <= MAX_MARGEN_LINEAS:
            raise ValueError(f"El margen debe estar entre 0 y {MAX_MARGEN_LINEAS}.")

        relativa = Path(ruta)
        if relativa.is_absolute() or relativa.drive or not relativa.parts or ".." in relativa.parts:
            raise ValueError("La ruta debe permanecer dentro del proyecto.")
        archivo = self.proyecto / relativa
        resuelta = archivo.resolve(strict=True)
        if not resuelta.is_relative_to(self.proyecto):
            raise ValueError("La ruta debe permanecer dentro del proyecto.")
        if not resuelta.is_file():
            raise ValueError("La ruta no corresponde a un archivo.")

        with closing(self._conexion()) as conexion:
            indexado = conexion.execute(
                "SELECT 1 FROM archivos WHERE ruta = ?",
                (relativa.as_posix(),),
            ).fetchone()
        if not indexado:
            raise ValueError("El archivo no pertenece al índice del proyecto.")
        if resuelta.stat().st_size > MAX_ARCHIVO:
            raise ValueError(f"El archivo supera el límite de {MAX_ARCHIVO} bytes.")

        lineas = resuelta.read_text(encoding="utf-8").splitlines()
        if linea > len(lineas):
            raise ValueError("La línea solicitada no existe en el archivo.")
        inicio = max(1, linea - margen)
        fin = min(len(lineas), linea + margen)
        contenido = "\n".join(lineas[inicio - 1:fin])
        truncado = len(contenido) > MAX_CARACTERES_LECTURA
        if truncado:
            contenido = contenido[:MAX_CARACTERES_LECTURA]
            lineas_devueltas = contenido.split("\n")
            if contenido.endswith("\n"):
                lineas_devueltas.pop()
            fin = inicio + len(lineas_devueltas) - 1
        return {
            "ruta": relativa.as_posix(),
            "linea_inicio": inicio,
            "linea_fin": fin,
            "contenido": contenido,
            "truncado": truncado,
        }
