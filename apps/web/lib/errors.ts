export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }

  get stale(): boolean {
    return this.status === 409;
  }

}
