import { Checkbox, Field, IconButton, Input, InputGroup, Link, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuEye, LuEyeOff, LuKeyRound } from "react-icons/lu";

import type { RequestTokenInfo } from "../../api/gen/types.gen";

/**
 * "Git token" for one write (storage.git.request_token): a masked field with a show/hide
 * toggle, collapsed under "Use my own token" when optional, always shown (and validated by
 * the dialog) when required. The value is controlled by the dialog's own React state.
 */
export function GitTokenField({
  info,
  value,
  onChange,
  invalid = false,
}: {
  info: RequestTokenInfo | null;
  value: string;
  onChange: (token: string) => void;
  /** The dialog tried to submit without a required token. */
  invalid?: boolean;
}) {
  const { t } = useTranslation("lifecycle");
  const [visible, setVisible] = useState(false);
  const [own, setOwn] = useState(false);
  if (!info || info.mode === "off") return null;
  const required = info.mode === "required";
  const open = required || own;
  const hint = info.hint;
  const kind = hint ? t(`gitToken.type.${hint.provider}`, { defaultValue: hint.token_type }) : null;

  return (
    <Stack gap={2} data-testid="git-token">
      {!required ? (
        <Checkbox.Root
          size="sm"
          checked={own}
          onCheckedChange={(e) => {
            const on = e.checked === true;
            setOwn(on);
            if (!on) {
              onChange("");
              setVisible(false);
            }
          }}
          data-testid="git-token-own"
        >
          <Checkbox.HiddenInput />
          <Checkbox.Control />
          <Checkbox.Label>
            {t("gitToken.useOwn")}
            <Text as="span" display="block" fontSize="xs" color="fg.muted" fontWeight="normal">
              {t("gitToken.useOwnHelp")}
            </Text>
          </Checkbox.Label>
        </Checkbox.Root>
      ) : null}
      {open ? (
        <Field.Root required={required} invalid={invalid}>
          <Field.Label>
            <LuKeyRound /> {t("gitToken.label")}
            {required ? <Field.RequiredIndicator /> : null}
          </Field.Label>
          <InputGroup
            endElement={
              <IconButton
                size="xs"
                variant="ghost"
                me="-2"
                aria-label={visible ? t("gitToken.hide") : t("gitToken.show")}
                aria-pressed={visible}
                title={visible ? t("gitToken.hide") : t("gitToken.show")}
                onClick={() => setVisible((v) => !v)}
                data-testid="git-token-toggle"
              >
                {visible ? <LuEyeOff /> : <LuEye />}
              </IconButton>
            }
          >
            <Input
              size="sm"
              type={visible ? "text" : "password"}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              autoCorrect="off"
              autoCapitalize="off"
              // Password managers must neither offer nor save it.
              data-1p-ignore=""
              data-lpignore="true"
              data-bwignore=""
              placeholder={t("gitToken.placeholder")}
              aria-describedby="git-token-help"
              data-testid="git-token-input"
            />
          </InputGroup>
          {invalid ? <Field.ErrorText>{t("gitToken.missing")}</Field.ErrorText> : null}
          <Field.HelperText id="git-token-help">
            {hint ? (
              <>
                {hint.scopes?.length
                  ? t("gitToken.hint", { kind, scopes: hint.scopes.join(", ") })
                  : t("gitToken.hintNoScopes", { kind })}{" "}
                {hint.docs_url ? (
                  <Link href={hint.docs_url} target="_blank" rel="noreferrer" colorPalette="blue">
                    {t("gitToken.docs")}
                  </Link>
                ) : null}
              </>
            ) : null}{" "}
            {t("gitToken.never")}
          </Field.HelperText>
        </Field.Root>
      ) : null}
    </Stack>
  );
}
