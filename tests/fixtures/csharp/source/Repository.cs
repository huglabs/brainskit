using System.Collections.Generic;

namespace Fixtures
{
    public interface IRepository
    {
        int Count();
    }

    public class Repository : IRepository
    {
        private readonly List<string> rows = new List<string>();

        public void Insert(string row)
        {
            rows.Add(row);
        }

        public int Count()
        {
            return rows.Count;
        }
    }

    public static class Seeder
    {
        public static int Seed(Repository repository)
        {
            repository.Insert("first");
            return repository.Count();
        }
    }
}
