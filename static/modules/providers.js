export async function loadProviders(apiPost) {
  const response = await apiPost('/api/providers/list', {});
  return response.providers || [];
}

export async function testProvider(apiPost, providerId) {
  return apiPost('/api/providers/test', { id: providerId });
}
