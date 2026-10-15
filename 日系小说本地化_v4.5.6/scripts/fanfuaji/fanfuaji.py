#!/usr/bin/env python3
"""
Fanfuaji (繁化姬) API Client (Lightweight Version)

A Python wrapper for the Fanhuaji Chinese terminology conversion API.
Supports conversion between simplified/traditional Chinese, regional variants,
and phonetic transcriptions.

This version uses standard library `urllib` instead of `requests` for zero dependencies.

Usage as library:
    from fanfuaji import convert_text, Converter
    
    result = convert_text("软件开发", Converter.TAIWAN)
    print(result)  # Output: 軟體開發

Usage as CLI:
    python fanfuaji.py "软件开发" --converter Taiwan
    python fanfuaji.py "软件" --converter Taiwan --protect "软件"
    python fanfuaji.py "哦" --converter Taiwan --post-replace "哦=喔"
    python fanfuaji.py --file input.txt --converter Taiwan
    python fanfuaji.py --file file:///path/to/input.txt --output output.txt
"""

import argparse
import json
import sys
import urllib.request
import urllib.parse
import urllib.error
import ssl
import mimetypes
from enum import Enum
from typing import Optional, Dict, List
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_INPUT_BYTES = 1024 * 1024
BLOCKED_BASENAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "service-account.json",
    "authorized_keys",
}
BLOCKED_EXTENSIONS = {
    ".p12",
    ".pfx",
    ".pem",
    ".key",
    ".sqlite",
    ".db",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".jar",
}
BLOCKED_SUBSTRINGS = (
    "credential",
    "secret",
    "token",
    "apikey",
    "api_key",
)


class Converter(str, Enum):
    """Available converter types."""
    SIMPLIFIED = "Simplified"
    TRADITIONAL = "Traditional"
    CHINA = "China"
    HONGKONG = "Hongkong"
    TAIWAN = "Taiwan"
    PINYIN = "Pinyin"
    BOPOMOFO = "Bopomofo"
    MARS = "Mars"
    WIKI_SIMPLIFIED = "WikiSimplified"
    WIKI_TRADITIONAL = "WikiTraditional"


@dataclass
class ConversionResult:
    """Result of text conversion."""
    text: str
    converter: str
    used_modules: List[str]
    exec_time: float
    
    def __str__(self) -> str:
        return self.text


class FanfuajiAPI:
    """Fanfuaji API client."""
    
    BASE_URL = "https://api.zhconvert.org"
    
    def __init__(self, api_key: str = "", verify_ssl: bool = True):
        """
        Initialize API client.
        
        Args:
            api_key: API key for commercial use (empty for free tier)
            verify_ssl: Whether to verify SSL certificate
        """
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self._headers = {
            "User-Agent": "FanfuajiPython/1.0 (Lightweight)"
        }
    
    def convert(
        self,
        text: str,
        converter: str,
        modules: Optional[Dict[str, int]] = None,
        user_pre_replace: Optional[Dict[str, str]] = None,
        user_post_replace: Optional[Dict[str, str]] = None,
        user_protect_replace: Optional[List[str]] = None,
        diff_enable: bool = False,
        prettify: bool = False,
        timeout: int = 10
    ) -> ConversionResult:
        """
        Convert text using specified converter.
        
        Args:
            text: Text to convert
            converter: Converter type (use Converter enum)
            modules: Module settings dict {module_name: 0|1}
            user_pre_replace: Replace before conversion {old: new}
            user_post_replace: Replace after conversion {old: new}
            user_protect_replace: Terms to protect from conversion
            diff_enable: Return diff between original and converted
            prettify: Prettify JSON response
            timeout: Request timeout in seconds
            
        Returns:
            ConversionResult with converted text and metadata
            
        Raises:
            ValueError: If API returns error
            RuntimeError: If request fails
        """
        data = {
            "converter": converter,
            "text": text,
            "apiKey": self.api_key,
            "diffEnable": str(diff_enable).lower(),
            "prettify": str(prettify).lower()
        }
        
        if modules:
            data["modules"] = json.dumps(modules)
        
        if user_pre_replace:
            data["userPreReplace"] = "\n".join(
                f"{old}={new}" for old, new in user_pre_replace.items()
            )
        
        if user_post_replace:
            data["userPostReplace"] = "\n".join(
                f"{old}={new}" for old, new in user_post_replace.items()
            )
        
        if user_protect_replace:
            data["userProtectReplace"] = "\n".join(user_protect_replace)
        
        encoded_data = urllib.parse.urlencode(data).encode('utf-8')
        req = urllib.request.Request(
            f"{self.BASE_URL}/convert",
            data=encoded_data,
            headers=self._headers,
            method="POST"
        )

        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
                response_body = response.read().decode('utf-8')
                try:
                    result = json.loads(response_body)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"Invalid JSON response: {e}")
                
                if result.get("code") != 0:
                    raise ValueError(f"API error: {result.get('msg', 'Unknown error')}")
                
                data_obj = result.get("data", {})
                
                return ConversionResult(
                    text=data_obj.get("text", ""),
                    converter=data_obj.get("converter", converter),
                    used_modules=data_obj.get("usedModules", []),
                    exec_time=result.get("execTime", 0.0)
                )

        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP request failed: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection failed: {e.reason}")
        except Exception as e:
            raise RuntimeError(f"Request failed: {e}")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def convert_text(
    text: str,
    converter: str = Converter.TAIWAN,
    **kwargs
) -> str:
    """
    Convert text using specified converter (simple interface).
    
    Args:
        text: Text to convert
        converter: Converter type (default: Taiwan)
        **kwargs: Additional parameters passed to FanfuajiAPI.convert()
        
    Returns:
        Converted text string
        
    Example:
        >>> convert_text("软件开发", Converter.TAIWAN)
        '軟體開發'
    """
    with FanfuajiAPI() as api:
        result = api.convert(text, converter, **kwargs)
        return result.text


def _normalize_local_path(path_or_uri: str) -> Path:
    local_path = path_or_uri
    if local_path.startswith("file://"):
        local_path = urllib.parse.unquote(local_path[7:])
    return Path(local_path)


def validate_input_path(
    file_path: str,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    allowed_dirs: Optional[List[str]] = None,
) -> Path:
    path = _normalize_local_path(file_path)

    if not path.exists():
        raise RuntimeError(f"File not found: {file_path}")
    if not path.is_file():
        raise RuntimeError(f"Not a file: {file_path}")

    resolved_path = path.resolve()
    lower_name = path.name.lower()
    lower_path = str(resolved_path).lower()

    if lower_name in BLOCKED_BASENAMES:
        raise RuntimeError(f"Input path blocked by secret-file policy: {path.name}")
    if any(marker in lower_name for marker in BLOCKED_SUBSTRINGS):
        raise RuntimeError(f"Input path blocked by secret-file policy: {path.name}")
    if any(marker in lower_path for marker in ("/.ssh/", "/.aws/", "/.gnupg/", "/.kube/")):
        raise RuntimeError(f"Input path blocked by secret-directory policy: {resolved_path}")

    suffix = path.suffix.lower()
    if suffix in BLOCKED_EXTENSIONS:
        raise RuntimeError(f"Input path blocked by extension policy: {suffix}")

    if allowed_dirs:
        resolved_allowed_dirs = [_normalize_local_path(p).resolve() for p in allowed_dirs]
        if not any(
            allowed_dir == resolved_path or allowed_dir in resolved_path.parents
            for allowed_dir in resolved_allowed_dirs
        ):
            raise RuntimeError(
                f"Input path blocked by allowlist policy: {resolved_path}. "
                "Use --allow-dir to include a parent directory."
            )

    file_size = path.stat().st_size
    if file_size > max_input_bytes:
        raise RuntimeError(
            f"Input size {file_size} exceeds max allowed size {max_input_bytes} bytes. "
            "Use --max-input-bytes only with explicit approval."
        )

    return path


def read_file_content(
    file_path: str,
    encoding: str = 'utf-8',
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    allowed_dirs: Optional[List[str]] = None,
) -> str:
    """
    Read file content from path or file:// URI.
    
    Args:
        file_path: File path or file:// URI
        encoding: Character encoding (default: utf-8)
        
    Returns:
        File content as string
        
    Raises:
        RuntimeError: If file cannot be read or is not a text file
    """
    path = validate_input_path(
        file_path,
        max_input_bytes=max_input_bytes,
        allowed_dirs=allowed_dirs,
    )
    
    try:
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type and not mime_type.startswith('text/'):
            raise RuntimeError(
                f"Binary file detected: {path.name}\n"
                f"File type: {mime_type}\n"
                f"Only text files are supported.\n"
                f"For binary files (e.g., xlsx, docx, pdf, images), please extract or convert to plain text first."
            )
        
        return path.read_text(encoding=encoding)
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"Failed to decode file with encoding '{encoding}': {path.name}\n"
            f"Error details: {e}\n"
            f"Try specifying a different encoding with --encoding flag.\n"
            f"Common encodings: utf-8, big5, gbk, gb2312, shift_jis"
        )
    except LookupError as e:
        raise RuntimeError(
            f"Unknown encoding '{encoding}'\n"
            f"Error details: {e}\n"
            f"Common encodings: utf-8, big5, gbk, gb2312, shift_jis"
        )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to read file: {e}")


def write_file_content(file_path: str, content: str) -> str:
    """
    Write content to file.
    
    Args:
        file_path: Output file path or file:// URI
        content: Content to write
        
    Returns:
        Actual file path written to
        
    Raises:
        RuntimeError: If file cannot be written
    """
    if file_path.startswith("file://"):
        file_path = urllib.parse.unquote(file_path[7:])
    
    try:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return str(path.absolute())
    except Exception as e:
        raise RuntimeError(f"Failed to write file: {e}")


def main():
    """CLI interface."""
    parser = argparse.ArgumentParser(
        description="Fanfuaji Chinese terminology converter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "软件开发" --converter Taiwan
  %(prog)s "软件" --converter Taiwan --protect "软件"
  %(prog)s "哦" --converter Taiwan --post-replace "哦=喔"
  %(prog)s "内存" --converter Taiwan --modules '{"GanToZuo": 0}'
  
  # File input/output
  %(prog)s --file input.txt --converter Taiwan
  %(prog)s --file file:///path/to/input.txt --output output.txt
  %(prog)s -f input.txt -o file:///path/to/output.txt -c Taiwan
  %(prog)s -f input.txt -o output.txt -c Taiwan --verbose
  
  # Different encodings
  %(prog)s -f big5_file.txt --encoding big5 -c Taiwan
  %(prog)s -f gbk_file.txt --encoding gbk -c Traditional
  
Available converters:
  Simplified, Traditional, China, Hongkong, Taiwan,
  Pinyin, Bopomofo, Mars, WikiSimplified, WikiTraditional
        """
    )
    
    parser.add_argument(
        "text",
        nargs='?',
        help="Text to convert (required if --file not specified)"
    )
    
    parser.add_argument(
        "-f", "--file",
        type=str,
        help="Input file path or file:// URI to convert (absolute path recommended)"
    )
    
    parser.add_argument(
        "-o", "--output",
        type=str,
        help="Output file path or file:// URI (default: print to stdout) (absolute path recommended)"
    )
    
    parser.add_argument(
        "-c", "--converter",
        default="Taiwan",
        choices=[c.value for c in Converter],
        help="Converter type (default: Taiwan)"
    )
    
    parser.add_argument(
        "--api-key",
        default="",
        help="API key for commercial use"
    )
    
    parser.add_argument(
        "--modules",
        type=str,
        help='Module settings as JSON string, e.g., \'{"GanToZuo": 0}\''
    )
    
    parser.add_argument(
        "--pre-replace",
        type=str,
        help='Pre-conversion replacements, e.g., "旧=新,另一个=替换"'
    )
    
    parser.add_argument(
        "--post-replace",
        type=str,
        help='Post-conversion replacements, e.g., "哦=喔,啰=囉"'
    )
    
    parser.add_argument(
        "--protect",
        type=str,
        help='Protected terms (comma-separated), e.g., "內存,硬盤,SSD"'
    )
    
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Request timeout in seconds (default: 10)"
    )
    
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed conversion information"
    )
    
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable SSL certificate verification"
    )
    
    parser.add_argument(
        "--encoding",
        type=str,
        default="utf-8",
        help="Input file character encoding (default: utf-8). Common: big5, gbk, gb2312, shift_jis"
    )

    parser.add_argument(
        "--max-input-bytes",
        type=int,
        default=DEFAULT_MAX_INPUT_BYTES,
        help="Maximum allowed input file size in bytes (default: 1048576)"
    )

    parser.add_argument(
        "--allow-dir",
        action="append",
        default=None,
        help="Restrict file input to this directory (can be repeated)"
    )
    
    args = parser.parse_args()
    
    # Validate input
    if not args.text and not args.file:
        parser.error("Either text argument or --file option is required")
    
    if args.text and args.file:
        parser.error("Cannot specify both text argument and --file option")
    
    # Read input
    if args.file:
        try:
            input_text = read_file_content(
                args.file,
                encoding=args.encoding,
                max_input_bytes=args.max_input_bytes,
                allowed_dirs=args.allow_dir,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        input_text = args.text
    
    kwargs = {}
    
    if args.modules:
        try:
            kwargs["modules"] = json.loads(args.modules)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in --modules: {args.modules}", file=sys.stderr)
            sys.exit(1)
    
    if args.pre_replace:
        kwargs["user_pre_replace"] = dict(
            pair.split("=", 1) for pair in args.pre_replace.split(",")
        )
    
    if args.post_replace:
        kwargs["user_post_replace"] = dict(
            pair.split("=", 1) for pair in args.post_replace.split(",")
        )
    
    if args.protect:
        kwargs["user_protect_replace"] = [
            term.strip() for term in args.protect.split(",")
        ]
    
    try:
        with FanfuajiAPI(
            api_key=args.api_key,
            verify_ssl=not args.no_verify_ssl
        ) as api:
            result = api.convert(
                text=input_text,
                converter=args.converter,
                timeout=args.timeout,
                **kwargs
            )
            
            # Output result
            if args.output:
                try:
                    output_path = write_file_content(args.output, result.text)
                    if args.verbose:
                        print(f"Converter: {result.converter}")
                        print(f"Used modules: {', '.join(result.used_modules)}")
                        print(f"Execution time: {result.exec_time:.3f}s")
                        print(f"Output written to: {output_path}")
                except RuntimeError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    sys.exit(1)
            else:
                if args.verbose:
                    print(f"Converter: {result.converter}")
                    print(f"Used modules: {', '.join(result.used_modules)}")
                    print(f"Execution time: {result.exec_time:.3f}s")
                    print(f"Result: {result.text}")
                else:
                    print(result.text)
                
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
