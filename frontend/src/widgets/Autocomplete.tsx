import { Box, Field, Input, Text } from "@chakra-ui/react";
import { type ReactNode, useId, useMemo, useRef, useState } from "react";

import { FIELD_ATTR, Help, Label } from "./fields";

export type AutocompleteOption = {
  value: string;
  label?: string | undefined;
  description?: string | undefined;
  /** Shown but not selectable (e.g. provider not installed). */
  disabled?: boolean | undefined;
};

type Props = {
  label: string;
  /** Visually hide the label (it stays the accessible name). */
  hideLabel?: boolean;
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  options: AutocompleteOption[];
  /** Called when an option is picked (defaults to onChange). */
  onSelect?: (option: AutocompleteOption) => void;
  placeholder?: string | undefined;
  help?: ReactNode;
  error?: string | undefined;
  required?: boolean;
  /** Type shown next to the label (default `str`). */
  dtype?: string | undefined;
  disabled?: boolean;
  mono?: boolean;
  field?: string | undefined;
  maxItems?: number;
  size?: "xs" | "sm";
  /** Clear the input after a pick (for "add item" pickers). */
  clearOnSelect?: boolean;
};

/**
 * Free-text input with a filtered suggestion list (ARIA combobox). Free text stays valid:
 * Airflow objects may come from env vars or secrets backends the API cannot list.
 */
export function Autocomplete({
  label,
  hideLabel,
  value,
  onChange,
  options,
  onSelect,
  placeholder,
  help,
  error,
  required,
  dtype,
  disabled,
  mono,
  field,
  maxItems = 50,
  size = "sm",
  clearOnSelect,
}: Props) {
  const id = useId();
  const listId = `${id}-list`;
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const blurTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const text = value ?? "";
  const matches = useMemo(() => {
    const q = text.trim().toLowerCase();
    const hits = q
      ? options.filter(
          (o) =>
            o.value.toLowerCase().includes(q) ||
            (o.label ?? "").toLowerCase().includes(q) ||
            (o.description ?? "").toLowerCase().includes(q),
        )
      : options;
    return hits.slice(0, maxItems);
  }, [options, text, maxItems]);
  const showList = open && matches.length > 0 && !(matches.length === 1 && matches[0]?.value === text);

  const pick = (o: AutocompleteOption) => {
    if (o.disabled) return;
    if (onSelect) onSelect(o);
    else onChange(o.value);
    if (clearOnSelect) onChange(null);
    setOpen(false);
  };

  return (
    <Field.Root
      invalid={!!error}
      required={required ?? false}
      disabled={disabled ?? false}
      position="relative"
      {...(field ? { [FIELD_ATTR]: field } : {})}
    >
      <Label label={label} required={required} dtype={dtype ?? "str"} hideLabel={hideLabel} />
      <Input
        size={size}
        role="combobox"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showList ? `${id}-opt-${active}` : undefined}
        autoComplete="off"
        fontFamily={mono ? "mono" : undefined}
        value={text}
        placeholder={placeholder}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          blurTimer.current = setTimeout(() => setOpen(false), 150);
        }}
        onChange={(e) => {
          onChange(e.target.value === "" ? null : e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setOpen(true);
            setActive((a) => Math.min(a + 1, matches.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((a) => Math.max(a - 1, 0));
          } else if (e.key === "Enter" && showList) {
            const o = matches[active];
            if (o) {
              e.preventDefault();
              pick(o);
            }
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
      />
      {showList ? (
        <Box
          id={listId}
          role="listbox"
          aria-label={label}
          position="absolute"
          top="100%"
          left={0}
          right={0}
          zIndex="dropdown"
          mt={1}
          maxH="240px"
          overflowY="auto"
          bg="bg.panel"
          borderWidth="1px"
          rounded="md"
          shadow="md"
          onMouseDown={(e) => {
            // keep focus in the input while clicking an option
            e.preventDefault();
            clearTimeout(blurTimer.current);
          }}
        >
          {matches.map((o, i) => (
            <Box
              key={o.value}
              id={`${id}-opt-${i}`}
              role="option"
              aria-selected={i === active}
              aria-disabled={o.disabled ?? false}
              px={2}
              py={1}
              cursor={o.disabled ? "not-allowed" : "pointer"}
              opacity={o.disabled ? 0.5 : 1}
              bg={i === active ? "bg.muted" : undefined}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(o)}
            >
              <Text fontSize="xs" fontFamily={mono ? "mono" : undefined} truncate>
                {o.label ?? o.value}
              </Text>
              {o.description ? (
                <Text fontSize="2xs" color="fg.muted" truncate>
                  {o.description}
                </Text>
              ) : null}
            </Box>
          ))}
        </Box>
      ) : null}
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}
