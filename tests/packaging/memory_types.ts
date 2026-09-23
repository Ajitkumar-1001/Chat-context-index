import {
  HistoryStore, BoundedProvider, ingest, index, prepareContext, packContext,
  type Context, type ContextItem, type ContextOptions, type InputMessage,
  type Provider, type ProviderRequest, type ProviderResponse,
} from "chat-context-index";

export async function consumer(store: HistoryStore): Promise<Context> {
  const inner: Provider = {
    async complete(request: ProviderRequest): Promise<ProviderResponse> {
      return { text: request.prompt };
    },
  };
  const provider = new BoundedProvider(inner, store.config);
  const messages: InputMessage[] = [{ role: "user", content: "Remember Oslo." }];
  await ingest(store, store.historyId, messages, "type-check", "turn");
  await index(store, provider);
  const options: ContextOptions = { mode: "tree", provider, maxChars: 1000 };
  const context: Context = await prepareContext(store, "Deployment?", options);
  const items: ContextItem[] = context.items;
  return packContext(items, options);
}
