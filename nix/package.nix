# Production build: Python deps come from nixpkgs (no pip, no venv).
# Keep this list in sync with requirements.txt, which only the dev shell uses.
{
  lib,
  python3,
  ffmpeg-headless,
  whisper-cpp,
  fetchurl,
  makeWrapper,
  stdenvNoCC,
}:
let
  python = python3.withPackages (
    ps: with ps; [
      fastapi
      uvicorn
      python-multipart
      httpx
      mcp
      python-dotenv
    ]
  );

  # Same file the dev shell's whisper-cpp-download-ggml-model fetches.
  whisperModel = fetchurl {
    name = "ggml-base.en.bin";
    url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";
    hash = "sha256-oDd5yG3zMjB19eeWyyzlAp8A7Ihp7uP9+4l6/jbG0AI=";
  };

  binPath = lib.makeBinPath [
    ffmpeg-headless
    whisper-cpp
  ];
in
stdenvNoCC.mkDerivation {
  pname = "pebble-ring";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.fileFilter (f: f.hasExt "py") ../app;
  };

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/pebble-ring $out/bin
    cp -r app $out/share/pebble-ring/

    # Server. Extra args (--host/--port) are appended by the caller.
    makeWrapper ${python}/bin/uvicorn $out/bin/pebble-ring \
      --add-flags "--app-dir $out/share/pebble-ring app.main:app" \
      --prefix PATH : ${binPath} \
      --set-default WHISPER_MODEL ${whisperModel} \
      --set-default WHISPER_MODEL_NAME base.en

    # Reprocess a saved recording by hand: pebble-ring-reprocess <file.m4a>
    makeWrapper ${python}/bin/python $out/bin/pebble-ring-reprocess \
      --add-flags "-m app.pipeline" \
      --prefix PYTHONPATH : $out/share/pebble-ring \
      --prefix PATH : ${binPath} \
      --set-default WHISPER_MODEL ${whisperModel}
    runHook postInstall
  '';

  passthru = { inherit python whisperModel; };

  meta = {
    description = "Webhook receiver for the Index 01 ring: whisper.cpp + LLM classification + actions";
    mainProgram = "pebble-ring";
    platforms = lib.platforms.unix;
  };
}
