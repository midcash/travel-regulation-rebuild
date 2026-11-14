export function element(id: string): HTMLElement;
export function element<T extends HTMLElement>(id: string, type: {new(): T}): T;
export function element(id: string, type = HTMLElement): HTMLElement {
  const node = document.getElementById(id);
  if (!(node instanceof type)) throw new Error(`Missing or invalid element #${id}`);
  return node;
}

export function setText(id: string, value: string): void {
  const node = element(id);
  if (node.textContent !== value) node.textContent = value;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
