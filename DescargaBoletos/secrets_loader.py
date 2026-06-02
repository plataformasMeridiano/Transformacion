"""
secrets_loader.py — Carga secrets de Azure Key Vault en os.environ.

Mapea nombres de Key Vault (guión) a variables de entorno (underscore).
Si no hay Managed Identity disponible (entorno local), no hace nada
y deja que load_dotenv() tome el control normalmente.

Uso:
    from secrets_loader import load_secrets
    load_secrets()  # llamar antes de load_dotenv() o de leer os.environ
"""

import logging
import os

logger = logging.getLogger(__name__)

VAULT_URL = "https://alycs-secrets.vault.azure.net/"

# Secrets que rotan — gestionados desde JSM + Key Vault.
# Formato: "NOMBRE-EN-VAULT" -> "NOMBRE_EN_ENV"
_SECRET_MAP = {
    "PUENTE-DOCUMENTO":        "PUENTE_DOCUMENTO",
    "PUENTE-USUARIO":          "PUENTE_USUARIO",
    "PUENTE-PASSWORD":         "PUENTE_PASSWORD",
    "ADCAP-USUARIO":           "ADCAP_USUARIO",
    "ADCAP-PASSWORD":          "ADCAP_PASSWORD",
    "BACS-USUARIO":            "BACS_USUARIO",
    "BACS-PASSWORD":           "BACS_PASSWORD",
    "MAX-USUARIO":             "MAX_USUARIO",
    "MAX-PASSWORD":            "MAX_PASSWORD",
    "CONOSUR-USUARIO":         "CONOSUR_USUARIO",
    "CONOSUR-PASSWORD":        "CONOSUR_PASSWORD",
    "CONOSUR-PAMAT-USUARIO":   "CONOSUR_PAMAT_USUARIO",
    "CONOSUR-PAMAT-PASSWORD":  "CONOSUR_PAMAT_PASSWORD",
    "CONOSUR-MANCIA-USUARIO":  "CONOSUR_MANCIA_USUARIO",
    "CONOSUR-MANCIA-PASSWORD": "CONOSUR_MANCIA_PASSWORD",
    "WIN-DOCUMENTO":           "WIN_DOCUMENTO",
    "WIN-USUARIO":             "WIN_USUARIO",
    "WIN-PASSWORD":            "WIN_PASSWORD",
    "METRO-DOCUMENTO":         "METRO_DOCUMENTO",
    "METRO-USUARIO":           "METRO_USUARIO",
    "METRO-PASSWORD":          "METRO_PASSWORD",
    "DHALMORE-USUARIO":        "DHALMORE_USUARIO",
    "DHALMORE-PASSWORD":       "DHALMORE_PASSWORD",
    "CRITERIA-USUARIO":        "CRITERIA_USUARIO",
    "CRITERIA-PASSWORD":       "CRITERIA_PASSWORD",
    "DA-VALORES-USUARIO":      "DA_VALORES_USUARIO",
    "DA-VALORES-PASSWORD":     "DA_VALORES_PASSWORD",
    "IEB-DOCUMENTO":           "IEB_DOCUMENTO",
    "IEB-USUARIO":             "IEB_USUARIO",
    "IEB-PASSWORD":            "IEB_PASSWORD",
    "ALLARIA-USUARIO":         "ALLARIA_USUARIO",
    "ALLARIA-PASSWORD":        "ALLARIA_PASSWORD",
    "ALLARIA-TOTP-SECRET":     "ALLARIA_TOTP_SECRET",
}


def load_secrets() -> bool:
    """
    Carga todos los secrets del Key Vault en os.environ.
    Retorna True si cargó desde el vault, False si usó fallback local.
    """
    try:
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient

        credential = ManagedIdentityCredential()
        client = SecretClient(vault_url=VAULT_URL, credential=credential)

        # Verificar conectividad con un secret de prueba antes de iterar
        client.get_secret(next(iter(_SECRET_MAP)))

        loaded = 0
        for vault_name, env_name in _SECRET_MAP.items():
            try:
                secret = client.get_secret(vault_name)
                os.environ[env_name] = secret.value
                loaded += 1
            except Exception as e:
                logger.warning("Key Vault: no se pudo leer '%s' — %s", vault_name, e)

        logger.info("Key Vault: %d/%d secrets cargados desde %s",
                    loaded, len(_SECRET_MAP), VAULT_URL)
        return True

    except ImportError:
        logger.debug("azure-keyvault-secrets no instalado — usando .env local")
    except Exception as e:
        logger.warning("Key Vault no disponible (%s) — usando .env local", e)

    return False
