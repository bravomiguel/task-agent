-- Vault helper RPCs for storing/reading secrets from Python agent and edge functions.
-- Uses vault.create_secret / vault.update_secret instead of raw INSERT to avoid
-- pgsodium _crypto_aead_det_noncegen permission errors.

-- Store a secret (upsert)
CREATE OR REPLACE FUNCTION set_vault_secret(p_name text, p_secret text)
RETURNS void AS $$
DECLARE
  existing_id uuid;
BEGIN
  SELECT id INTO existing_id FROM vault.secrets WHERE name = p_name LIMIT 1;
  IF existing_id IS NOT NULL THEN
    PERFORM vault.update_secret(existing_id, p_secret, p_name);
  ELSE
    PERFORM vault.create_secret(p_secret, p_name);
  END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Read a secret
CREATE OR REPLACE FUNCTION get_vault_secret(p_name text)
RETURNS text AS $$
  SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = p_name LIMIT 1;
$$ LANGUAGE sql SECURITY DEFINER;

-- Delete a secret
CREATE OR REPLACE FUNCTION delete_vault_secret(p_name text)
RETURNS void AS $$
  DELETE FROM vault.secrets WHERE name = p_name;
$$ LANGUAGE sql SECURITY DEFINER;
