# Credential security

* Exchange keys are Fernet-encrypted before they touch the database (`credentials.encrypted_blob`), with `encryption_key_version` and `last4`. API responses show only the last 4 characters.
* Keys come from `ENCRYPTION_KEYS="1:<key>,2:<key>"`; the highest version encrypts. To rotate the master key: add a new version, run `reencrypt_all()`, then retire the old one. To rotate an exchange key: `rotate_credentials` (the new key must also pass the no-withdraw check).
* Withdraw permission: a key is rejected if the exchange says Withdraw is on **or cannot say**. Enabling an exchange for LIVE re-reads permissions from the exchange. Only Binance has a permission check (never run against the real exchange); Bybit/OKX are stubs, so their keys are always rejected.
* The audit scrubber redacts any metadata key containing secret, password, token, api key, passphrase, authorization. Vault errors never include key material.
* `.env` is git-ignored; compose no longer ships default passwords.

**Limits:** the master key lives in an environment variable (no HSM/KMS). Anyone with the server environment plus the database can decrypt credentials.
