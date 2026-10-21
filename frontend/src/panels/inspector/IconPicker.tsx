import {
  Box,
  Button,
  Dialog,
  Field,
  HStack,
  IconButton,
  Input,
  NativeSelect,
  Popover,
  Portal,
  SegmentGroup,
  SimpleGrid,
  Stack,
  Text,
} from "@chakra-ui/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuImage, LuImagePlus, LuRotateCcw, LuTrash2 } from "react-icons/lu";
import { useAccess } from "../../lib/access";

import {
  assignIconMutation,
  deleteIconMutation,
  listIconsQueryKey,
  listOperatorsQueryKey,
  uploadIconMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import type { IconInfo } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { IconImage, useIcons } from "../../canvas/icons";
import {
  type IconBackground,
  type IconFit,
  type IconRenderOptions,
  iconSlug,
  loadImage,
  renderIcon,
} from "../../lib/iconImage";
import { toaster } from "../../ui/toaster";

type Scope = "node" | "operator";

/**
 * Canvas icon of a node: bundled integration logo or custom upload. For operator nodes the
 * choice can be the default of every node of that operator class (e.g. your own operators).
 */
export function IconField({
  value,
  effective,
  onChange,
  operator,
}: {
  /** The node's own icon (overrides the default). */
  value: string | null | undefined;
  /** What the node shows when it has none (operator/integration default). */
  effective: string | null | undefined;
  onChange: (icon: string | undefined) => void;
  /** Operator class FQN: enables "all nodes of this operator". */
  operator?: string | undefined;
}) {
  const { t } = useTranslation("inspector");
  const qc = useQueryClient();
  const icons = useIcons();
  const [open, setOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const access = useAccess();
  const canUpload = access.allows("custom_icons", "icon:write");
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<Scope>("node");
  const assign = useMutation({
    ...assignIconMutation(),
    onSuccess: () => qc.invalidateQueries({ queryKey: listOperatorsQueryKey() }),
    onError: (e) => toaster.create({ type: "error", title: t("icon.assignFailed"), description: errorMessage(e) }),
  });
  const remove = useMutation({
    ...deleteIconMutation(),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: listIconsQueryKey() });
      await qc.invalidateQueries({ queryKey: listOperatorsQueryKey() });
    },
  });
  const cls = operator?.split(".").pop() ?? "";

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = icons.data ?? [];
    const list = q ? all.filter((i) => i.name.includes(q)) : all;
    return [...list.filter((i) => i.kind === "custom"), ...list.filter((i) => i.kind === "builtin")];
  }, [icons.data, query]);

  const pick = (icon: IconInfo | null) => {
    if (scope === "operator" && operator) {
      assign.mutate({ body: { operator, icon: icon?.id ?? null } });
      onChange(undefined); // the node follows the operator default
    } else {
      onChange(icon?.id);
    }
    setOpen(false);
  };

  return (
    <Field.Root>
      <Field.Label fontSize="xs">{t("icon.label")}</Field.Label>
      <HStack gap={2}>
        <Box
          w="36px"
          h="36px"
          borderWidth="1px"
          rounded="md"
          display="grid"
          placeItems="center"
          bg="bg.subtle"
          data-testid="icon-current"
        >
          <IconImage id={value ?? effective} size={24} fallback={<LuImage />} />
        </Box>
        <Popover.Root open={open} onOpenChange={(e) => setOpen(e.open)} lazyMount unmountOnExit>
          <Popover.Trigger asChild>
            <Button size="xs" variant="outline">
              <LuImage /> {t("icon.change")}
            </Button>
          </Popover.Trigger>
          <Portal>
            <Popover.Positioner>
              <Popover.Content w="360px" className="nowheel">
                <Popover.Body p={3}>
                  <Stack gap={2}>
                    {operator ? (
                      <SegmentGroup.Root
                        size="xs"
                        value={scope}
                        onValueChange={(e) => setScope(e.value === "operator" ? "operator" : "node")}
                      >
                        <SegmentGroup.Indicator />
                        <SegmentGroup.Items
                          items={[
                            { value: "node", label: t("icon.scopeNode") },
                            { value: "operator", label: t("icon.scopeOperator", { name: cls }) },
                          ]}
                        />
                      </SegmentGroup.Root>
                    ) : null}
                    <HStack>
                      <Input
                        size="xs"
                        placeholder={t("icon.search")}
                        aria-label={t("icon.search")}
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                      {canUpload ? (
                        <Button size="xs" variant="outline" onClick={() => setUploading(true)}>
                          <LuImagePlus /> {t("icon.upload")}
                        </Button>
                      ) : null}
                    </HStack>
                    <Box maxH="260px" overflowY="auto">
                      <SimpleGrid columns={8} gap={1}>
                        {filtered.map((i) => (
                          <Box key={i.id} position="relative" className="group">
                            <IconButton
                              size="sm"
                              variant={(value ?? effective) === i.id ? "subtle" : "ghost"}
                              aria-label={i.name}
                              title={i.name}
                              onClick={() => pick(i)}
                            >
                              <IconImage id={i.id} size={22} />
                            </IconButton>
                            {i.kind === "custom" ? (
                              <IconButton
                                size="2xs"
                                variant="solid"
                                colorPalette="red"
                                position="absolute"
                                top="-4px"
                                right="-4px"
                                minW="14px"
                                h="14px"
                                opacity={0}
                                _groupHover={{ opacity: 1 }}
                                aria-label={t("icon.delete", { name: i.name })}
                                onClick={() => remove.mutate({ path: { icon_id: i.id } })}
                              >
                                <LuTrash2 size={9} />
                              </IconButton>
                            ) : null}
                          </Box>
                        ))}
                      </SimpleGrid>
                      {icons.data && !filtered.length ? (
                        <Text fontSize="xs" color="fg.muted" p={2}>
                          {t("icon.none")}
                        </Text>
                      ) : null}
                    </Box>
                    <Button size="xs" variant="ghost" alignSelf="start" onClick={() => pick(null)}>
                      <LuRotateCcw /> {t("icon.default")}
                    </Button>
                  </Stack>
                </Popover.Body>
              </Popover.Content>
            </Popover.Positioner>
          </Portal>
        </Popover.Root>
        {value ? (
          <Button size="xs" variant="ghost" onClick={() => onChange(undefined)}>
            <LuRotateCcw /> {t("icon.reset")}
          </Button>
        ) : null}
      </HStack>
      {uploading ? (
        <IconUploadDialog
          onClose={() => setUploading(false)}
          onUploaded={(icon) => {
            setUploading(false);
            pick(icon);
          }}
        />
      ) : null}
    </Field.Root>
  );
}

const PREVIEW_SIZES = [16, 24, 32, 64];

/** Choose an image, frame it (fit, padding, zoom, background), preview it, upload a 64 px PNG. */
function IconUploadDialog({ onClose, onUploaded }: { onClose: () => void; onUploaded: (icon: IconInfo) => void }) {
  const { t } = useTranslation("inspector");
  const qc = useQueryClient();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [opts, setOpts] = useState<
    Required<Pick<IconRenderOptions, "fit" | "padding" | "zoom" | "background" | "color">>
  >({
    fit: "contain",
    padding: 0.08,
    zoom: 1,
    background: "transparent",
    color: "#ffffff",
  });
  const upload = useMutation({
    ...uploadIconMutation(),
    onSuccess: async (icon) => {
      await qc.invalidateQueries({ queryKey: listIconsQueryKey() });
      toaster.create({ type: "success", title: t("icon.uploaded", { name: icon.name }) });
      onUploaded(icon);
    },
    onError: (e) => setError(errorMessage(e)),
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!img || !canvas) return;
    renderIcon(img, canvas, { size: 64, ...opts });
    setPreview(canvas.toDataURL("image/png"));
  }, [img, opts]);

  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()} size="sm" lazyMount>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{t("icon.uploadTitle")}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap={3}>
                <Text fontSize="xs" color="fg.muted">
                  {t("icon.uploadHelp")}
                </Text>
                <Input
                  type="file"
                  size="sm"
                  accept="image/png,image/jpeg,image/webp,image/gif,image/svg+xml,.svg"
                  aria-label={t("icon.file")}
                  p={1}
                  onChange={async (e) => {
                    const file = e.target.files?.[0];
                    if (!file) return;
                    setError(null);
                    try {
                      setImg(await loadImage(file));
                      if (!name) setName(iconSlug(file.name));
                    } catch {
                      setError(t("icon.unreadable"));
                    }
                  }}
                />
                <canvas ref={canvasRef} hidden />
                {preview ? (
                  <HStack gap={3} align="end" wrap="wrap" data-testid="icon-previews">
                    {PREVIEW_SIZES.map((s) => (
                      <Stack key={s} gap={1} align="center">
                        <HStack gap={1}>
                          {(["#ffffff", "#1f2937"] as const).map((bg) => (
                            <Box key={bg} p={1} rounded="sm" borderWidth="1px" style={{ background: bg }}>
                              <img src={preview} width={s} height={s} alt="" style={{ display: "block" }} />
                            </Box>
                          ))}
                        </HStack>
                        <Text fontSize="2xs" color="fg.muted">
                          {s}px
                        </Text>
                      </Stack>
                    ))}
                  </HStack>
                ) : null}
                <SimpleGrid columns={2} gap={2}>
                  <Field.Root>
                    <Field.Label fontSize="xs">{t("icon.fit")}</Field.Label>
                    <NativeSelect.Root size="xs">
                      <NativeSelect.Field
                        value={opts.fit}
                        onChange={(e) => setOpts({ ...opts, fit: e.target.value as IconFit })}
                      >
                        <option value="contain">{t("icon.fitContain")}</option>
                        <option value="cover">{t("icon.fitCover")}</option>
                      </NativeSelect.Field>
                    </NativeSelect.Root>
                  </Field.Root>
                  <Field.Root>
                    <Field.Label fontSize="xs">{t("icon.background")}</Field.Label>
                    <HStack gap={1}>
                      <NativeSelect.Root size="xs">
                        <NativeSelect.Field
                          value={opts.background}
                          onChange={(e) => setOpts({ ...opts, background: e.target.value as IconBackground })}
                        >
                          <option value="transparent">{t("icon.bgTransparent")}</option>
                          <option value="white">{t("icon.bgWhite")}</option>
                          <option value="rounded">{t("icon.bgRounded")}</option>
                        </NativeSelect.Field>
                      </NativeSelect.Root>
                      {opts.background === "rounded" ? (
                        <input
                          type="color"
                          aria-label={t("icon.bgColor")}
                          value={opts.color}
                          onChange={(e) => setOpts({ ...opts, color: e.target.value })}
                          style={{ width: 28, height: 24, border: "none", background: "none" }}
                        />
                      ) : null}
                    </HStack>
                  </Field.Root>
                  <Field.Root>
                    <Field.Label fontSize="xs">
                      {t("icon.padding")} · {Math.round(opts.padding * 100)}%
                    </Field.Label>
                    <input
                      type="range"
                      min={0}
                      max={30}
                      value={Math.round(opts.padding * 100)}
                      aria-label={t("icon.padding")}
                      onChange={(e) => setOpts({ ...opts, padding: Number(e.target.value) / 100 })}
                    />
                  </Field.Root>
                  <Field.Root>
                    <Field.Label fontSize="xs">
                      {t("icon.zoom")} · {opts.zoom.toFixed(1)}×
                    </Field.Label>
                    <input
                      type="range"
                      min={50}
                      max={200}
                      value={Math.round(opts.zoom * 100)}
                      aria-label={t("icon.zoom")}
                      onChange={(e) => setOpts({ ...opts, zoom: Number(e.target.value) / 100 })}
                    />
                  </Field.Root>
                </SimpleGrid>
                <Field.Root required invalid={!!name && iconSlug(name) !== name}>
                  <Field.Label fontSize="xs">{t("icon.name")}</Field.Label>
                  <Input size="sm" value={name} onChange={(e) => setName(e.target.value)} placeholder="gitlab" />
                  <Field.HelperText fontSize="2xs">
                    {t("icon.nameHelp", { id: `custom:${iconSlug(name)}` })}
                  </Field.HelperText>
                </Field.Root>
                {error ? (
                  <Text fontSize="xs" color="red.500">
                    {error}
                  </Text>
                ) : null}
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button size="sm" variant="ghost" onClick={onClose}>
                {t("icon.cancel")}
              </Button>
              <Button
                size="sm"
                colorPalette="blue"
                disabled={!preview || !name.trim()}
                loading={upload.isPending}
                onClick={() => preview && upload.mutate({ body: { name: iconSlug(name), data: preview } })}
              >
                {t("icon.save")}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
