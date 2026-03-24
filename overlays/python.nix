# Python package overlay for OpenHands dependencies.
#
# Adds missing packages and bumps outdated ones that aren't
# at the versions required by the SDK.
{ pkgs }:

final: prev:

let
  # Helper to build a simple Python package from PyPI sdist
  buildFromPyPI = { pname, version, hash, deps ? [], buildInputs ? [],
                     format ? null, build-system ? [ final.setuptools ],
                     pythonImportsCheck ? [ (builtins.replaceStrings [ "-" ] [ "_" ] pname) ],
                     meta ? {} }:
    final.buildPythonPackage ({
      inherit pname version pythonImportsCheck;
      src = pkgs.fetchPypi {
        inherit version hash;
        pname = builtins.replaceStrings [ "-" ] [ "_" ] pname;
      };
      inherit build-system;
      dependencies = deps;
      # Most of these are pure Python, skip tests by default
      # (tests are run at the SDK level where it matters)
      doCheck = false;
      inherit meta;
    } // (if format != null then { inherit format; } else { pyproject = true; }));

in
{
  # ============================================================
  # OpenTelemetry stack bump (1.34.0 -> 1.39.1)
  # Required by lmnr >= 0.7.24 (which pins semantic-conventions==0.60b1)
  # ============================================================

  # All otel overrides need sourceRoot reset because nixpkgs builds them
  # from a monorepo tarball with subdirectories, but PyPI sdists are flat.
  opentelemetry-api = prev.opentelemetry-api.overridePythonAttrs (old: rec {
    version = "1.39.1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_api";
      inherit version;
      hash = "sha256-+96MgOG5N6LGHyA0fpHAwYoZQM7PAS1i5lp8rwiWfJw=";
    };
  });

  opentelemetry-semantic-conventions = prev.opentelemetry-semantic-conventions.overridePythonAttrs (old: rec {
    version = "0.60b1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_semantic_conventions";
      inherit version;
      hash = "sha256-h8IotaBmm3SMdtdt9sNkw2nCjxxGXlD2YeOXN+hLyVM=";
    };
  });

  opentelemetry-sdk = prev.opentelemetry-sdk.overridePythonAttrs (old: rec {
    version = "1.39.1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_sdk";
      inherit version;
      hash = "sha256-z01FY8r3v/kGyfeWfiviLQ1rNJuQi+DZD7IcjpyZXMY=";
    };
    dependencies = (old.dependencies or []) ++ [
      final.opentelemetry-api
      final.opentelemetry-semantic-conventions
    ];
  });

  opentelemetry-proto = prev.opentelemetry-proto.overridePythonAttrs (old: rec {
    version = "1.39.1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_proto";
      inherit version;
      hash = "sha256-bI4FFE/A0+1NIsIonGsSbgO80Oan2g8Wzt0uHCdy4sg=";
    };
  });

  opentelemetry-exporter-otlp-proto-common = prev.opentelemetry-exporter-otlp-proto-common.overridePythonAttrs (old: rec {
    version = "1.39.1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_exporter_otlp_proto_common";
      inherit version;
      hash = "sha256-djNw1HN6WXQciaZ7UPnjknFjnuSvyZna3+doVBwCdGQ=";
    };
  });

  opentelemetry-exporter-otlp-proto-http = prev.opentelemetry-exporter-otlp-proto-http.overridePythonAttrs (old: rec {
    version = "1.39.1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_exporter_otlp_proto_http";
      inherit version;
      hash = "sha256-Mb2rl0XHCc6QpJoGJMK9RF0xooujQnWVGmo2LRagucs=";
    };
  });

  opentelemetry-exporter-otlp-proto-grpc = prev.opentelemetry-exporter-otlp-proto-grpc.overridePythonAttrs (old: rec {
    version = "1.39.1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_exporter_otlp_proto_grpc";
      inherit version;
      hash = "sha256-dy6xySh0hdYl5NvpyHmJjlJT/qER2RgRQPUSkbX+w60=";
    };
  });

  opentelemetry-instrumentation = prev.opentelemetry-instrumentation.overridePythonAttrs (old: rec {
    version = "0.60b1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_instrumentation";
      inherit version;
      hash = "sha256-V93Hl0xus1hlrwQm0aFxMriLLthYaJf+4Yf9W4lEvWo=";
    };
    dependencies = (old.dependencies or []) ++ [
      final.opentelemetry-api
      final.opentelemetry-semantic-conventions
    ];
  });

  opentelemetry-test-utils = prev.opentelemetry-test-utils.overridePythonAttrs (old: rec {
    version = "0.60b1";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "opentelemetry_test_utils";
      inherit version;
      hash = "sha256-UkZ+oJm3n+3SdMyq1+XRB4OkdRJOFzyn/EDokClh9u8=";
    };
  });
  # ============================================================
  # New OpenTelemetry packages (not in nixpkgs)
  # ============================================================

  opentelemetry-semantic-conventions-ai = buildFromPyPI {
    pname = "opentelemetry-semantic-conventions-ai";
    version = "0.4.13";
    hash = "sha256-lO+p+0/6wYxF9Uo6M4/+t+7bfhu00Ud4bncgLhWfADY=";
    build-system = [ final.poetry-core ];
    pythonImportsCheck = [ "opentelemetry.semconv_ai" ];
    deps = [ final.opentelemetry-api ];
  };

  opentelemetry-instrumentation-threading = buildFromPyPI {
    pname = "opentelemetry-instrumentation-threading";
    version = "0.60b1";
    hash = "sha256-ILGKaKvlgB+pR0M2t8J0h9SvPgC2b2qHNOT911yLC0M=";
    build-system = [ final.hatchling ];
    pythonImportsCheck = [ "opentelemetry.instrumentation.threading" ];
    deps = [
      final.opentelemetry-api
      final.opentelemetry-instrumentation
      final.wrapt
    ];
  };

  # ============================================================
  # Version bumps for existing nixpkgs packages
  # ============================================================

  litellm = prev.litellm.overridePythonAttrs (old: rec {
    version = "1.82.5";
    src = pkgs.fetchPypi {
      pname = "litellm";
      inherit version;
      hash = "sha256-eYiptIyMzZ5evO2ApN/OnOhwg7MDw/ZwgkUKStbdMS8=";
    };
    # litellm 1.82+ ships a broken symlink litellm_enterprise -> enterprise/litellm_enterprise
    # The enterprise directory doesn't exist in the open-source release.
    pythonImportsCheck = [ "litellm" ];
    # Remove broken symlink after wheel install, before fixup checks
    postInstall = (old.postInstall or "") + ''
      find $out -name litellm_enterprise -type l -delete
    '';
    # Also disable the broken symlinks check as a safety net
    dontCheckForBrokenSymlinks = true;
  });

  fastmcp = prev.fastmcp.overridePythonAttrs (old: rec {
    version = "3.1.0";
    src = pkgs.fetchPypi {
      pname = "fastmcp";
      inherit version;
      hash = "sha256-4lJkeUxzS5l3UCpRRmlh7uz/kqDC87ScQMBwmTYo1tA=";
    };
    dependencies = [
      final.authlib
      final.cyclopts
      final.exceptiongroup
      final.httpx
      final.jsonref
      final.jsonschema-path
      final.mcp
      final.openapi-pydantic
      final.opentelemetry-api
      final.packaging
      final.platformdirs
      final.py-key-value-aio
      final.pydantic
      final.pyperclip
      final.python-dotenv
      final.pyyaml
      final.rich
      final.uncalled-for
      final.uvicorn
      final.watchfiles
      final.websockets
    ];
    # Tests require opentelemetry.sdk and other test deps; tested at SDK level
    doCheck = false;
  });

  libtmux = prev.libtmux.overridePythonAttrs (old: rec {
    version = "0.55.0";
    src = pkgs.fetchPypi {
      pname = "libtmux";
      inherit version;
      hash = "sha256-zcSqVksjJWGNc9V8sNfZJHXQICbborlqlPh60yjn550=";
    };
  });

  py-key-value-aio = prev.py-key-value-aio.overridePythonAttrs (old: rec {
    version = "0.4.4";
    sourceRoot = null;
    src = pkgs.fetchPypi {
      pname = "py_key_value_aio";
      inherit version;
      hash = "sha256-4wEuYkPtfMCbsFRXvU0DsbpcKxyocACWs5J9t5/7vlU=";
    };
    dependencies = [
      final.beartype
      final.typing-extensions
      final.cachetools  # needed for py-key-value-aio[memory] extra
    ];
    # Test structure changed in 0.4.x, disable tests (tested at SDK level)
    doCheck = false;
    disabledTestPaths = [];
  });
  # ============================================================
  # New packages not in nixpkgs
  # ============================================================

  agent-client-protocol = buildFromPyPI {
    pname = "agent-client-protocol";
    version = "0.8.1";
    hash = "sha256-G78VZjv1H2SUJZf2OOMqYoTF2pGAVdlnLTUQ6WUUPb0=";
    build-system = [ final.pdm-backend ];
    pythonImportsCheck = [ "acp" ];
    deps = [ final.pydantic ];
  };

  func-timeout = buildFromPyPI {
    pname = "func-timeout";
    version = "4.3.5";
    hash = "sha256-dM08Qo7JT07fuoH5svFJBIRtX/zMJ8kkM7i1k5tVdd0=";
    format = "setuptools";
    build-system = [ final.setuptools ];
  };

  tom-swe = buildFromPyPI {
    pname = "tom-swe";
    version = "1.0.3";
    hash = "sha256-V8l9AQTlY/Fb057a8qpqxMPpREr9Q3+5JFhwDSLGwPU=";
    build-system = [ final.hatchling ];
    deps = [
      final.jinja2
      final.json-repair
      final.litellm
      final.pydantic
      final.python-dotenv
      final.tiktoken
      final.tqdm
    ];
  };

  uncalled-for = buildFromPyPI {
    pname = "uncalled-for";
    version = "0.2.0";
    hash = "sha256-tPj9vOwyjFoROAfWU+BBxQlEc91K+nw0WZrOacy35p8=";
    build-system = [ final.hatchling final.hatch-vcs ];
  };

  lmnr = final.buildPythonPackage {
    pname = "lmnr";
    version = "0.7.44";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "lmnr";
      version = "0.7.44";
      hash = "sha256-ABzbh1VK/MGv/3IzP86CBZGllbMJYkhkNxhlkM6xwgs=";
    };
    # Relax uv-build upper bound (0.10.0 works fine)
    postPatch = ''
      substituteInPlace pyproject.toml \
        --replace-fail 'requires = ["uv_build>=0.9.7,<0.10"]' 'requires = ["uv_build>=0.9.7"]'
    '';
    build-system = [ final.uv-build ];
    dependencies = [
      final.pydantic
      final.python-dotenv
      final.opentelemetry-api
      final.opentelemetry-sdk
      final.opentelemetry-exporter-otlp-proto-http
      final.opentelemetry-exporter-otlp-proto-grpc
      final.opentelemetry-instrumentation
      final.opentelemetry-semantic-conventions
      final.opentelemetry-semantic-conventions-ai
      final.opentelemetry-instrumentation-threading
      final.tqdm
      final.tenacity
      final.grpcio
      final.httpx
      final.orjson
      final.packaging
    ];
    pythonImportsCheck = [ "lmnr" ];
    doCheck = false;
  };

  # ============================================================
  # CLI dependencies
  # ============================================================

  textual = prev.textual.overridePythonAttrs (old: rec {
    version = "8.1.1";
    src = pkgs.fetchPypi {
      pname = "textual";
      inherit version;
      hash = "sha256-7vAlamEx8GogrXV2QSE4wfMPkt3u3QVZU8CNlwRLwxc=";
    };
  });

  textual-autocomplete = buildFromPyPI {
    pname = "textual-autocomplete";
    version = "4.0.6";
    hash = "sha256-K6Lw12e+RIDsrLPksTDPBzQOAzw1APxCT+2RJdJ6RYY=";
    build-system = [ final.hatchling ];
    deps = [
      final.textual
      final.typing-extensions
    ];
  };

  textual-serve = buildFromPyPI {
    pname = "textual-serve";
    version = "1.1.3";
    hash = "sha256-+PY2ri9f1lG3nZZUc8PpOD01Ic34lvm8KJcJGF2j9oM=";
    build-system = [ final.hatchling ];
    deps = [
      final.aiohttp
      final.aiohttp-jinja2
      final.jinja2
      final.rich
      final.textual
    ];
  };

  streamingjson = buildFromPyPI {
    pname = "streamingjson";
    version = "0.0.5";
    hash = "sha256-k7AofOStQIkwj75XRbi/1js55EWm84kUdOqEkShKI+M=";
  };

  # ============================================================
  # Server (openhands-ai) dependencies
  # ============================================================

  httpx-aiohttp = buildFromPyPI {
    pname = "httpx-aiohttp";
    version = "0.1.12";
    hash = "sha256-gf7sUf2CwOz6DpqvGxpsJZEmDV4ry+t+sCd6eOYQ3yw=";
    build-system = [ final.hatchling final.hatch-fancy-pypi-readme ];
    deps = [ final.aiohttp final.httpx ];
  };

  scantree = buildFromPyPI {
    pname = "scantree";
    version = "0.0.4";
    hash = "sha256-Fb1cskSDsE2yxwZTYE6Oo1IumAh9t+OKuEgvBTmEwKw=";
    format = "setuptools";
    build-system = [ final.setuptools final.versioneer ];
    deps = [ final.attrs final.pathspec ];
  };

  dirhash = buildFromPyPI {
    pname = "dirhash";
    version = "0.5.0";
    hash = "sha256-5gdg8Ksuk12MsIiSPqLGSSOY3KQs7Hhd93iYX9TNU4Y=";
    format = "setuptools";
    build-system = [ final.setuptools final.versioneer ];
    deps = [ final.scantree ];
  };

  pybase62 = final.buildPythonPackage {
    pname = "pybase62";
    version = "1.0.0";
    format = "wheel";
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/py3/p/pybase62/pybase62-1.0.0-py3-none-any.whl";
      hash = "sha256-YFOa2Vbsnp3gkbx66IyVULwvoX9QMFDPNNAht15zyyc=";
    };
    doCheck = false;
    pythonImportsCheck = [ "base62" ];
  };

  # openhands-aci: agent computer interface (code editing tools)
  openhands-aci = final.buildPythonPackage {
    pname = "openhands-aci";
    version = "0.3.3";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "openhands_aci";
      version = "0.3.3";
      hash = "sha256-Vn/GW7iB4+pWyYf0JRyPcD08iPrplAK0bqfcxI2FrbI=";
    };
    build-system = [ final.poetry-core ];
    # Relax strict pins (libcst==1.5.0, tree-sitter<0.25)
    pythonRelaxDeps = true;
    dependencies = with final; [
      beautifulsoup4
      binaryornot
      cachetools
      charset-normalizer
      flake8
      gitpython
      grep-ast
      libcst
      mammoth
      markdownify
      matplotlib
      networkx
      openpyxl
      pandas
      pdfminer-six
      puremagic
      pydantic
      pydub
      pypdf
      python-pptx
      rapidfuzz
      requests
      speechrecognition
      tree-sitter
      tree-sitter-language-pack
      whatthepatch
      xlrd
      youtube-transcript-api
    ];
    doCheck = false;
    pythonImportsCheck = [ "openhands_aci" ];
  };

  # browsergym-core: browser automation interface (used by browsing agent)
  # playwright is included as Python lib only — no browser binaries bundled.
  browsergym-core = buildFromPyPI {
    pname = "browsergym-core";
    version = "0.13.3";
    hash = "sha256-rFA2tXTIwUrEoMCdpXigoAtYTW9bXtm/eiR+JPTZ0vg=";
    build-system = [ final.hatchling final.hatch-requirements-txt ];
    pythonImportsCheck = [ "browsergym.core" ];
    deps = with final; [
      beautifulsoup4
      gymnasium
      lxml
      numpy
      pillow
      playwright
      pyparsing
    ];
  };

}
